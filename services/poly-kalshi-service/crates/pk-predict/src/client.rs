use std::collections::HashSet;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use rand::Rng as _;
use reqwest::Method;
use serde_json::{Value, json};

use crate::model::{PredictMarket, markets_from_response};
use crate::sign::{OrderInput, PredictSigner, Side};

pub struct Client {
    http: reqwest::Client,
    base: String,
    api_key: String,
    signer: Option<Arc<PredictSigner>>,
    jwt: parking_lot::RwLock<Option<String>>,
    request_id: AtomicU64,
}

pub struct Submit<'a> {
    pub market: &'a PredictMarket,
    pub token_id: &'a str,
    pub side: Side,
    pub price: f64,
    pub quantity: f64,
    pub strategy: &'a str,
    pub expiration: u64,
    pub slippage_bps: Option<u64>,
    pub post_only: bool,
}

pub struct Submitted {
    pub response: Value,
    /// Exact unsigned order fields needed for an on-chain cancellation. This
    /// value is process-local and must never be journalled or exposed by HTTP.
    pub order: Value,
}

impl Client {
    pub fn new(base: &str, api_key: &str, signer: Option<Arc<PredictSigner>>) -> anyhow::Result<Self> {
        Ok(Self {
            http: reqwest::Client::builder()
                .connect_timeout(std::time::Duration::from_secs(3))
                .timeout(std::time::Duration::from_secs(8))
                .build()?,
            base: base.trim_end_matches('/').into(),
            api_key: api_key.into(),
            signer,
            jwt: parking_lot::RwLock::new(None),
            request_id: AtomicU64::new(1),
        })
    }

    async fn request(
        &self,
        method: Method,
        path: &str,
        body: Option<&Value>,
        jwt: bool,
    ) -> anyhow::Result<Value> {
        let mut req =
            self.http.request(method, format!("{}{}", self.base, path)).header("x-api-key", &self.api_key);
        if jwt {
            let token = self
                .jwt
                .read()
                .clone()
                .ok_or_else(|| anyhow::anyhow!("wallet authentication is not established"))?;
            req = req.bearer_auth(token);
        }
        if let Some(value) = body {
            req = req.json(value);
        }
        let response = req.send().await?;
        let status = response.status();
        let value: Value = response.json().await.unwrap_or_else(|_| json!({}));
        anyhow::ensure!(
            status.is_success(),
            "Predict HTTP {}: {}",
            status.as_u16(),
            public_api_error(&value)
        );
        Ok(value)
    }

    pub async fn authenticate(&self) -> anyhow::Result<()> {
        let signer =
            self.signer.as_ref().ok_or_else(|| anyhow::anyhow!("Predict signer is not configured"))?;
        let response = self.request(Method::GET, "/v1/auth/message", None, false).await?;
        let message = response
            .pointer("/data/message")
            .or_else(|| response.get("message"))
            .and_then(Value::as_str)
            .ok_or_else(|| anyhow::anyhow!("auth message missing"))?;
        let signature = signer.sign_auth_message(message)?;
        let response = self
            .request(
                Method::POST,
                "/v1/auth",
                Some(&json!({
                    "signer": signer.account().to_checksum(None), "message": message, "signature": signature,
                })),
                false,
            )
            .await?;
        let token = response
            .pointer("/data/token")
            .or_else(|| response.get("token"))
            .and_then(Value::as_str)
            .ok_or_else(|| anyhow::anyhow!("auth token missing"))?;
        *self.jwt.write() = Some(token.into());
        Ok(())
    }

    pub fn jwt(&self) -> Option<String> {
        self.jwt.read().clone()
    }

    /// All OPEN crypto up/down markets. The API orders by creation, so a
    /// single first page is only old CLOSED markets; follow `cursor`.
    pub async fn markets(&self) -> anyhow::Result<Vec<PredictMarket>> {
        let mut all = Vec::new();
        let mut cursor: Option<String> = None;
        for _ in 0..20 {
            let mut path = "/v1/markets?first=100&marketVariant=CRYPTO_UP_DOWN&status=OPEN".to_string();
            if let Some(c) = &cursor {
                path.push_str(&format!("&after={c}"));
            }
            let value = self.request(Method::GET, &path, None, false).await?;
            let page = markets_from_response(&value);
            let empty = page.is_empty();
            all.extend(page);
            cursor = value.get("cursor").and_then(Value::as_str).map(str::to_owned);
            if empty || cursor.as_deref().is_none_or(str::is_empty) {
                break;
            }
        }
        Ok(all)
    }

    /// One market by id (also after it closes: `status`, outcome `WON`/`LOST`).
    pub async fn market(&self, market_id: &str) -> anyhow::Result<Value> {
        self.request(Method::GET, &format!("/v1/markets/{market_id}"), None, false).await
    }

    pub async fn orderbook(&self, market_id: u64) -> anyhow::Result<Value> {
        self.request(Method::GET, &format!("/v1/markets/{market_id}/orderbook"), None, false).await
    }

    pub async fn orders(&self) -> anyhow::Result<Value> {
        self.request(Method::GET, "/v1/orders", None, true).await
    }
    pub async fn positions(&self) -> anyhow::Result<Value> {
        self.request(Method::GET, "/v1/positions", None, true).await
    }

    pub async fn submit(&self, input: Submit<'_>) -> anyhow::Result<Submitted> {
        let signer =
            self.signer.as_ref().ok_or_else(|| anyhow::anyhow!("Predict signer is not configured"))?;
        let salt = rand::thread_rng().gen_range(1..2_147_483_648_u64);
        let signed = signer.sign_order(&OrderInput {
            token_id: input.token_id,
            price: input.price,
            quantity: input.quantity,
            expiration: input.expiration,
            fee_rate_bps: input.market.fee_rate_bps,
            side: input.side,
            salt,
            is_neg_risk: input.market.is_neg_risk,
            is_yield_bearing: input.market.is_yield_bearing,
        })?;
        // Market buys need isMinAmountOut so the venue applies slippageBps
        // against the taker amount; without it slippage is silently ignored.
        let market = input.strategy == "MARKET";
        // `pricePerShare` is an 18-decimal integer like the order amounts
        // (0.42 USDT = "420000000000000000"); a decimal string is refused
        // with HTTP 400 "invalid numeric value".
        let body = json!({"data": {
            "order": signed.clone(), "pricePerShare": price_wei(input.price), "strategy": input.strategy,
            "slippageBps": input.slippage_bps, "isFillOrKill": market,
            "isPostOnly": input.post_only, "isMinAmountOut": market, "selfTradePrevention": "CANCEL_MAKER"
        }});
        let response = self.request(Method::POST, "/v1/orders", Some(&body), true).await?;
        Ok(Submitted { response, order: signed })
    }

    /// Fast off-book removal by numeric order ID. This does not invalidate
    /// the order on-chain; callers must batch an on-chain cancellation after.
    pub async fn remove(&self, ids: &[String]) -> anyhow::Result<Value> {
        self.request(Method::POST, "/v1/orders/remove", Some(&json!({"data": {"ids": ids}})), true).await
    }

    pub async fn cancel_all_off_book(&self) -> anyhow::Result<Value> {
        let orders = self.orders().await?;
        // Already filled/cancelled rows come back in `noop`; removal is idempotent.
        let ids: Vec<String> = find_array(&orders)
            .iter()
            .filter_map(|o| {
                o.get("id").map(|id| id.as_str().map(str::to_owned).unwrap_or_else(|| id.to_string()))
            })
            .collect();
        if ids.is_empty() { Ok(json!({"removed": 0})) } else { self.remove(&ids).await }
    }

    /// `cancel_all_off_book` for the open orders on these outcome tokens
    /// only, so requoting one market leaves the others' quotes resting.
    pub async fn cancel_tokens_off_book(&self, tokens: &HashSet<String>) -> anyhow::Result<Value> {
        let orders = self.orders().await?;
        let ids: Vec<String> = find_array(&orders)
            .iter()
            .filter(|o| order_token(o).is_some_and(|token| tokens.contains(&token)))
            .filter_map(|o| {
                o.get("id").map(|id| id.as_str().map(str::to_owned).unwrap_or_else(|| id.to_string()))
            })
            .collect();
        if ids.is_empty() { Ok(json!({"removed": 0})) } else { self.remove(&ids).await }
    }

    pub fn next_request_id(&self) -> u64 {
        self.request_id.fetch_add(1, Ordering::Relaxed)
    }
}

/// An open order row's outcome token (the signed order nests it under `order`).
fn order_token(row: &Value) -> Option<String> {
    let raw = row.get("order").unwrap_or(row);
    raw.get("tokenId").map(|value| value.as_str().map(str::to_owned).unwrap_or_else(|| value.to_string()))
}

fn find_array(value: &Value) -> &[Value] {
    value
        .pointer("/data/orders")
        .or_else(|| value.get("orders"))
        .or_else(|| value.get("data"))
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or_default()
}

fn public_api_error(value: &Value) -> String {
    value
        .pointer("/error/message")
        .or_else(|| value.pointer("/data/message"))
        .or_else(|| value.get("message"))
        .and_then(Value::as_str)
        .unwrap_or("request rejected")
        .chars()
        .take(180)
        .collect()
}

/// A price in USDT as Predict's 18-decimal integer string, rounded to 1e-9
/// first exactly as the signed order amounts are.
fn price_wei(price: f64) -> String {
    let nanos = (price * 1_000_000_000.0).round().max(0.0) as u128;
    (nanos * 1_000_000_000).to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::Router;
    use axum::extract::State;
    use axum::routing::{get, post};
    use serde_json::json;

    #[test]
    fn open_order_rows_name_their_token_nested_or_flat() {
        assert_eq!(order_token(&json!({"id":"1","order":{"tokenId":"42"}})).as_deref(), Some("42"));
        assert_eq!(order_token(&json!({"id":"1","tokenId":7})).as_deref(), Some("7"));
        assert_eq!(order_token(&json!({"id":"1"})), None);
    }

    const KEY: &str = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318";
    const ACCOUNT: &str = "0x1111111111111111111111111111111111111111";

    #[derive(Clone, Default)]
    struct Mock {
        seen: Arc<parking_lot::Mutex<Vec<Value>>>,
        mode: Arc<parking_lot::Mutex<String>>,
    }

    fn market() -> PredictMarket {
        serde_json::from_value(json!({
            "id": 7, "title":"BTC", "question":"up?", "tradingStatus":"OPEN", "isNegRisk":false,
            "isYieldBearing":true, "feeRateBps":100, "outcomes":[{"name":"Yes","indexSet":1,"onChainId":"9"},{"name":"No","indexSet":2,"onChainId":"10"}],
            "kalshiMarketTicker":"KXBTC15M-ABC", "decimalPrecision":2, "marketVariant":"CRYPTO_UP_DOWN",
            "variantData":{"priceFeedProvider":"PYTH"}, "endsAt":"2027-01-01T00:00:00Z"
        }))
        .unwrap()
    }

    async fn serve(mock: Mock) -> String {
        async fn auth_message(State(mock): State<Mock>) -> axum::Json<Value> {
            if mock.mode.lock().as_str() == "flaky" {
                return axum::Json(json!({"unexpected": true}));
            }
            axum::Json(json!({"data": {"message": "sign-me"}}))
        }
        async fn auth(State(mock): State<Mock>, body: axum::Json<Value>) -> axum::Json<Value> {
            mock.seen.lock().push(body.0);
            axum::Json(json!({"data": {"token": "jwt-token"}}))
        }
        async fn orders(State(mock): State<Mock>, body: axum::Json<Value>) -> axum::Json<Value> {
            mock.seen.lock().push(body.0);
            axum::Json(json!({"data": {"order": {"id": "42", "hash": "0xabc"}}}))
        }
        async fn list(State(mock): State<Mock>) -> axum::Json<Value> {
            if mock.mode.lock().as_str() == "fail" {
                return axum::Json(json!({}));
            }
            axum::Json(json!({"data": {"orders": [{"id": "1"}, {"id": 2}]}}))
        }
        async fn remove(State(mock): State<Mock>, body: axum::Json<Value>) -> axum::Json<Value> {
            mock.seen.lock().push(body.0);
            axum::Json(json!({"success": true, "removed": ["1"], "noop": []}))
        }
        async fn boom() -> (axum::http::StatusCode, axum::Json<Value>) {
            (axum::http::StatusCode::INTERNAL_SERVER_ERROR, axum::Json(json!({"message": "bad"})))
        }
        async fn markets(uri: axum::http::Uri) -> axum::Json<Value> {
            let row = |id: u64| {
                json!({"id": id, "title":"BTC", "question":"up?", "tradingStatus":"OPEN", "isNegRisk":false,
                    "isYieldBearing":false, "feeRateBps":100, "outcomes":[{"name":"Up","indexSet":1,"onChainId":"9"},{"name":"Down","indexSet":2,"onChainId":"10"}],
                    "kalshiMarketTicker":null, "decimalPrecision":2, "marketVariant":"CRYPTO_UP_DOWN",
                    "seriesSlug":"btc-updown-15m", "categorySlug":"btc-updown-15m-1793017800",
                    "variantData":{"priceFeedProvider":"CHAINLINK"}})
            };
            if uri.query().is_some_and(|q| q.contains("after=page2")) {
                axum::Json(json!({"success": true, "cursor": null, "data": [row(8)]}))
            } else {
                assert!(uri.query().is_some_and(|q| q.contains("status=OPEN")));
                axum::Json(json!({"success": true, "cursor": "page2", "data": [row(7)]}))
            }
        }
        let app = Router::new()
            .route("/v1/auth/message", get(auth_message))
            .route("/v1/auth", post(auth))
            .route("/v1/orders", post(orders).get(list))
            .route("/v1/orders/remove", post(remove))
            .route("/v1/markets", get(markets))
            .route("/v1/boom", get(boom))
            .with_state(mock);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://127.0.0.1:{}", listener.local_addr().unwrap().port());
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        base
    }

    fn client(base: &str) -> Client {
        let signer = Arc::new(PredictSigner::new(KEY, ACCOUNT, crate::sign::BNB_MAINNET).unwrap());
        Client::new(base, "api-key", Some(signer)).unwrap()
    }

    #[tokio::test]
    async fn authenticate_signs_challenge_with_smart_account_envelope() {
        let mock = Mock::default();
        let base = serve(mock.clone()).await;
        let client = client(&base);
        client.authenticate().await.unwrap();
        assert_eq!(client.jwt().as_deref(), Some("jwt-token"));
        let seen = mock.seen.lock();
        assert_eq!(seen.len(), 1);
        assert_eq!(seen[0]["signer"], ACCOUNT);
        assert_eq!(seen[0]["message"], "sign-me");
        assert!(seen[0]["signature"].as_str().unwrap().starts_with("0x01"));
    }

    #[tokio::test]
    async fn markets_paginates_open_pages_by_cursor() {
        let mock = Mock::default();
        let base = serve(mock.clone()).await;
        let client = client(&base);
        let markets = client.markets().await.unwrap();
        assert_eq!(markets.iter().map(|m| m.id).collect::<Vec<_>>(), vec![7, 8]);
        assert!(markets.iter().all(|m| m.supported(&["BTC".into()])));
    }

    #[tokio::test]
    async fn missing_challenge_is_an_error_not_an_empty_signature() {
        let mock = Mock { mode: Arc::new(parking_lot::Mutex::new("flaky".into())), ..Mock::default() };
        let base = serve(mock.clone()).await;
        let client = client(&base);
        assert!(client.authenticate().await.is_err());
        assert!(client.jwt().is_none());
    }

    #[tokio::test]
    async fn submit_posts_signed_limit_with_maker_fields() {
        let mock = Mock::default();
        let base = serve(mock.clone()).await;
        let client = client(&base);
        client.jwt.write().replace("jwt-token".into());
        let market = market();
        let submitted = client
            .submit(Submit {
                market: &market,
                token_id: "9",
                side: Side::Buy,
                price: 0.42,
                quantity: 1.0,
                strategy: "LIMIT",
                expiration: 999,
                slippage_bps: None,
                post_only: true,
            })
            .await
            .unwrap();
        assert_eq!(submitted.response["data"]["order"]["id"], "42");
        // The process-local order carries the exact unsigned integers for on-chain cancel.
        assert_eq!(submitted.order["tokenId"], "9");
        let body = mock.seen.lock().last().cloned().unwrap();
        assert_eq!(body["data"]["strategy"], "LIMIT");
        assert_eq!(body["data"]["pricePerShare"], "420000000000000000");
        assert_eq!(body["data"]["isPostOnly"], true);
        assert_eq!(body["data"]["isFillOrKill"], false);
        assert_eq!(body["data"]["isMinAmountOut"], false);
        assert_eq!(body["data"]["selfTradePrevention"], "CANCEL_MAKER");
        assert!(body["data"]["order"]["signature"].as_str().unwrap().starts_with("0x01"));
        assert!(body["data"]["order"]["hash"].as_str().unwrap().starts_with("0x"));
    }

    #[tokio::test]
    async fn market_submit_enables_slippage_protection() {
        let mock = Mock::default();
        let base = serve(mock.clone()).await;
        let client = client(&base);
        client.jwt.write().replace("jwt-token".into());
        let market = market();
        client
            .submit(Submit {
                market: &market,
                token_id: "9",
                side: Side::Buy,
                price: 0.42,
                quantity: 1.0,
                strategy: "MARKET",
                expiration: 999,
                slippage_bps: Some(100),
                post_only: false,
            })
            .await
            .unwrap();
        let body = mock.seen.lock().last().cloned().unwrap();
        assert_eq!(body["data"]["strategy"], "MARKET");
        assert_eq!(body["data"]["isFillOrKill"], true);
        assert_eq!(body["data"]["isMinAmountOut"], true);
        assert_eq!(body["data"]["slippageBps"], 100);
    }

    #[tokio::test]
    async fn cancel_all_collects_ids_and_removes_by_ids() {
        let mock = Mock::default();
        let base = serve(mock.clone()).await;
        let client = client(&base);
        client.jwt.write().replace("jwt-token".into());
        let reply = client.cancel_all_off_book().await.unwrap();
        assert_eq!(reply["success"], true);
        let body = mock.seen.lock().last().cloned().unwrap();
        assert_eq!(body["data"]["ids"], json!(["1", "2"]));
    }

    #[tokio::test]
    async fn server_errors_surface_without_body_leak() {
        let mock = Mock::default();
        let base = serve(mock.clone()).await;
        let client = client(&base);
        let error = client.request(Method::GET, "/v1/boom", None, false).await.unwrap_err();
        assert_eq!(error.to_string(), "Predict HTTP 500: bad");
    }
}
