//! The Limitless order path (Base, chain 8453). Nothing here is on the
//! Polymarket path; only `pk-limitless` uses it.
//!
//! - Requests are HMAC-SHA256 signed ([`Hmac`]): `lmts-api-key`,
//!   `lmts-timestamp` (ISO-8601, within 30 s of the server) and
//!   `lmts-signature` = base64(HMAC(base64-decoded secret,
//!   `ts\nMETHOD\npath?query\nbody`)).
//! - Orders are EIP-712 signed by the EOA ([`LimSigner`]): domain "Limitless
//!   CTF Exchange", version 1, chain 8453, `verifyingContract` = the market's
//!   `venue.exchange`. `nonce` and `expiration` are 0; `feeRateBps` is 0 on
//!   the new (COLLATERAL) venues, where the taker fee is paid in USDC.
//! - Amounts are 6-decimal raw units and `price × contracts` must be exact,
//!   so prices are whole thousandths and sizes whole shares ([`LimOrder`]).
//! - FAK and FOK orders, and GTC orders that cross, are held for the market's
//!   `takerDelayMs` and answered `DELAYED`; the result comes from
//!   `POST /orders/status/batch`. Post-only orders and cancels are never held.
//! - [`LimChain`] sends the one-time approvals (USDC to the exchange and the
//!   fee module, Conditional Tokens to the exchange) and redeems resolved
//!   positions with `redeemPositions`. Limitless never redeems for an EOA.

use std::str::FromStr;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

use alloy::primitives::{Address, B256, U256};
use alloy::providers::{DynProvider, Provider, ProviderBuilder};
use alloy::network::{Ethereum, Network, TransactionBuilder};
use alloy::signers::SignerSync as _;
use alloy::signers::local::PrivateKeySigner;
use alloy::sol_types::{Eip712Domain, SolCall, SolStruct};
use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use hmac::{Hmac as HmacImpl, Mac};
use reqwest::Method;
use serde::Serialize;
use serde_json::{Value, json};
use sha2::Sha256;

use crate::transport::WarmHttp;

pub const CHAIN_ID: u64 = 8453;
type TxRequest = <Ethereum as Network>::TransactionRequest;
pub const USDC: &str = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
pub const CTF: &str = "0xC9c98965297Bc527861c898329Ee280632B76e18";
pub const DOMAIN_NAME: &str = "Limitless CTF Exchange";

alloy::sol! {
    #[derive(Debug)]
    struct Order {
        uint256 salt;
        address maker;
        address signer;
        address taker;
        uint256 tokenId;
        uint256 makerAmount;
        uint256 takerAmount;
        uint256 expiration;
        uint256 nonce;
        uint256 feeRateBps;
        uint8 side;
        uint8 signatureType;
    }

    function approve(address spender, uint256 amount) returns (bool);
    function allowance(address owner, address spender) returns (uint256);
    function balanceOf(address owner) returns (uint256);
    function setApprovalForAll(address operator, bool approved);
    function isApprovedForAll(address owner, address operator) returns (bool);
    function redeemPositions(address collateralToken, bytes32 parentCollectionId, bytes32 conditionId, uint256[] indexSets);
}

/// ISO-8601 UTC with milliseconds, as the venue checks it.
pub fn iso_now() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
}

pub struct Hmac {
    token_id: String,
    secret: Vec<u8>,
}

impl Hmac {
    pub fn new(token_id: &str, secret_b64: &str) -> anyhow::Result<Self> {
        let secret = STANDARD.decode(secret_b64.trim())?;
        anyhow::ensure!(!secret.is_empty(), "LIMITLESS_API_SECRET is empty");
        Ok(Self { token_id: token_id.trim().to_owned(), secret })
    }

    pub fn signature(&self, ts: &str, method: &str, path_and_query: &str, body: &str) -> String {
        let mut mac = HmacImpl::<Sha256>::new_from_slice(&self.secret).expect("any key length");
        mac.update(format!("{ts}\n{method}\n{path_and_query}\n{body}").as_bytes());
        STANDARD.encode(mac.finalize().into_bytes())
    }

    pub fn headers(&self, method: &str, path_and_query: &str, body: &str) -> Vec<(&'static str, String)> {
        let ts = iso_now();
        let sig = self.signature(&ts, method, path_and_query, body);
        vec![("lmts-api-key", self.token_id.clone()), ("lmts-timestamp", ts), ("lmts-signature", sig)]
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum LimSide {
    Buy,
    Sell,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum LimTif {
    Gtc,
    Fak,
}

/// One order in exact units: the price in thousandths, whole shares.
#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize)]
pub struct LimOrder {
    pub slug: String,
    pub token_id: String,
    pub side: LimSide,
    pub price_milli: u32,
    pub shares: u64,
    pub tif: LimTif,
    pub post_only: bool,
}

impl LimOrder {
    /// `(makerAmount, takerAmount)` in raw 6-decimal units.
    pub fn amounts(&self) -> (u64, u64) {
        let shares_raw = self.shares * 1_000_000;
        let usd_raw = u64::from(self.price_milli) * self.shares * 1_000;
        match self.side {
            LimSide::Buy => (usd_raw, shares_raw),
            LimSide::Sell => (shares_raw, usd_raw),
        }
    }

    pub fn price(&self) -> f64 {
        f64::from(self.price_milli) / 1e3
    }
}

/// A price as whole thousandths, rounded toward the side that never overpays:
/// down for a buy limit, up for a sell limit.
pub fn price_milli(price: f64, side: LimSide) -> u32 {
    let m = price * 1e3;
    let m = match side {
        LimSide::Buy => (m + 1e-6).floor(),
        LimSide::Sell => (m - 1e-6).ceil(),
    };
    m.clamp(1.0, 999.0) as u32
}

pub struct LimSigner {
    key: PrivateKeySigner,
}

impl LimSigner {
    pub fn new(private_key: &str) -> anyhow::Result<Self> {
        Ok(Self { key: PrivateKeySigner::from_str(private_key.trim())? })
    }

    pub fn address(&self) -> Address {
        self.key.address()
    }

    pub fn domain(exchange: Address) -> Eip712Domain {
        Eip712Domain::new(
            Some(DOMAIN_NAME.into()),
            Some("1".into()),
            Some(U256::from(CHAIN_ID)),
            Some(exchange),
            None,
        )
    }

    /// The signed `order` object of a `POST /orders` body.
    pub fn sign(&self, exchange: Address, o: &LimOrder, salt: u64) -> anyhow::Result<Value> {
        let (maker_amount, taker_amount) = o.amounts();
        let side: u8 = match o.side {
            LimSide::Buy => 0,
            LimSide::Sell => 1,
        };
        let me = self.address();
        let order = Order {
            salt: U256::from(salt),
            maker: me,
            signer: me,
            taker: Address::ZERO,
            tokenId: U256::from_str(&o.token_id)?,
            makerAmount: U256::from(maker_amount),
            takerAmount: U256::from(taker_amount),
            expiration: U256::ZERO,
            nonce: U256::ZERO,
            feeRateBps: U256::ZERO,
            side,
            signatureType: 0,
        };
        let hash = order.eip712_signing_hash(&Self::domain(exchange));
        let sig = self.key.sign_hash_sync(&hash)?;
        Ok(json!({
            "salt": salt.to_string(),
            "maker": me.to_checksum(None),
            "signer": me.to_checksum(None),
            "taker": Address::ZERO.to_checksum(None),
            "tokenId": o.token_id,
            "makerAmount": maker_amount,
            "takerAmount": taker_amount,
            "expiration": "0",
            "nonce": 0,
            "price": o.price(),
            "feeRateBps": 0,
            "side": side,
            "signatureType": 0,
            "signature": format!("0x{}", alloy::hex::encode(sig.as_bytes())),
        }))
    }
}

/// Decision → signed → written → headers → acknowledgement, as `order_timing` has it.
#[derive(Clone, Debug, Default, Serialize)]
pub struct LimTiming {
    pub sign_us: u64,
    pub decision_to_write_us: u64,
    pub write_to_headers_us: u64,
    pub headers_to_ack_us: u64,
    pub decision_to_ack_us: u64,
    pub http: u16,
}

#[derive(Clone, Debug, Serialize)]
pub struct Posted {
    pub timing: LimTiming,
    pub ok: bool,
    pub order_id: Option<String>,
    pub client_order_id: String,
    /// `execution.settlementStatus`: DELAYED for a held taker, UNMATCHED for a resting GTC.
    pub status: Option<String>,
    pub eligible_at: Option<String>,
    pub error: Option<String>,
    pub body: Value,
}

/// Shares and USDC an execution reports (`totalsRaw`, in units, not raw).
#[derive(Clone, Copy, Debug, Default, PartialEq, Serialize)]
pub struct Filled {
    pub shares: f64,
    pub usd_gross: f64,
    pub usd_fee: f64,
}

pub fn filled(execution: &Value) -> Filled {
    let raw = |k: &str| {
        execution
            .pointer(&format!("/totalsRaw/{k}"))
            .and_then(|v| v.as_f64().or_else(|| v.as_str().and_then(|s| s.parse().ok())))
            .unwrap_or(0.0)
            / 1e6
    };
    Filled { shares: raw("contractsNet"), usd_gross: raw("usdGross"), usd_fee: raw("usdFee") }
}

pub struct LimClient {
    http: Arc<WarmHttp>,
    hmac: Hmac,
    signer: LimSigner,
    owner_id: i64,
    salt: AtomicU64,
}

impl LimClient {
    /// `GET /profiles/me` gives the `ownerId` every order must carry; it also
    /// proves the token and the clock before anything is sent.
    pub async fn connect(http: Arc<WarmHttp>, hmac: Hmac, signer: LimSigner) -> anyhow::Result<Self> {
        let mut this = Self { http, hmac, signer, owner_id: 0, salt: AtomicU64::new(0) };
        let profile = this.get("/profiles/me").await?;
        this.owner_id = profile
            .get("id")
            .and_then(Value::as_i64)
            .ok_or_else(|| anyhow::anyhow!("GET /profiles/me has no id: {}", short(&profile)))?;
        let wallet = profile.get("account").and_then(Value::as_str).unwrap_or_default();
        anyhow::ensure!(
            wallet.eq_ignore_ascii_case(&this.signer.address().to_string()),
            "the Limitless profile's wallet {wallet} is not the signing key's {}",
            this.signer.address()
        );
        if let Some(mode) = profile.get("tradeWalletOption").and_then(Value::as_str) {
            anyhow::ensure!(mode == "eoa", "the profile trades from its {mode}; API orders need PUT /profiles {{\"tradeWalletOption\":\"eoa\"}}");
        }
        let start = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_nanos() as u64;
        this.salt.store(start, Ordering::Relaxed);
        Ok(this)
    }

    pub fn address(&self) -> Address {
        self.signer.address()
    }

    pub fn owner_id(&self) -> i64 {
        self.owner_id
    }

    pub fn http(&self) -> &Arc<WarmHttp> {
        &self.http
    }

    async fn request(&self, method: Method, path: &str, body: Option<String>) -> anyhow::Result<(u16, Value)> {
        let text = body.clone().unwrap_or_default();
        let headers = self.hmac.headers(method.as_str(), path, &text);
        let resp = self.http.send(method, path, &headers, body.map(String::into_bytes)).await?;
        let status = resp.status().as_u16();
        let bytes = resp.bytes().await?;
        Ok((status, serde_json::from_slice(&bytes).unwrap_or(Value::Null)))
    }

    pub async fn get(&self, path: &str) -> anyhow::Result<Value> {
        let (status, body) = self.request(Method::GET, path, None).await?;
        anyhow::ensure!(status == 200, "GET {path}: {status} {}", short(&body));
        Ok(body)
    }

    /// Sign and post one order, timed from `t_decision`.
    pub async fn post(&self, exchange: Address, o: &LimOrder, t_decision: Instant) -> anyhow::Result<Posted> {
        let salt = self.salt.fetch_add(1, Ordering::Relaxed);
        let t_sign = Instant::now();
        let order = self.signer.sign(exchange, o, salt)?;
        let sign_us = t_sign.elapsed().as_micros() as u64;
        let client_order_id = format!("pk-{salt}");
        let mut body = json!({
            "order": order,
            "orderType": o.tif,
            "marketSlug": o.slug,
            "ownerId": self.owner_id,
            "clientOrderId": client_order_id,
            "timestamp": chrono::Utc::now().timestamp_millis(),
            "recvWindow": 2000,
            "stpPolicy": "cancel_taker",
        });
        if o.post_only {
            body["postOnly"] = true.into();
        }
        let text = body.to_string();
        let headers = self.hmac.headers("POST", "/orders", &text);
        let t_write = Instant::now();
        let resp = self.http.send(Method::POST, "/orders", &headers, Some(text.into_bytes())).await?;
        let t_headers = Instant::now();
        let http = resp.status().as_u16();
        let bytes = resp.bytes().await?;
        let t_ack = Instant::now();
        let reply: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
        let us = |a: Instant, b: Instant| b.saturating_duration_since(a).as_micros() as u64;
        let timing = LimTiming {
            sign_us,
            decision_to_write_us: us(t_decision, t_write),
            write_to_headers_us: us(t_write, t_headers),
            headers_to_ack_us: us(t_headers, t_ack),
            decision_to_ack_us: us(t_decision, t_ack),
            http,
        };
        let ok = (200..300).contains(&http);
        let execution = reply.get("execution").cloned().unwrap_or(Value::Null);
        Ok(Posted {
            timing,
            ok,
            order_id: reply
                .pointer("/order/id")
                .or_else(|| reply.get("id"))
                .and_then(Value::as_str)
                .map(str::to_owned),
            client_order_id,
            status: execution.get("settlementStatus").and_then(Value::as_str).map(str::to_owned),
            eligible_at: execution.get("eligibleAt").and_then(Value::as_str).map(str::to_owned),
            error: (!ok).then(|| short(&reply)),
            body: reply,
        })
    }

    /// The latest known state of one order: `(settlementStatus, execution)`.
    pub async fn status(&self, order_id: &str) -> anyhow::Result<(Option<String>, Value)> {
        let body = json!({"items": [{"orderId": order_id}]}).to_string();
        let (status, reply) = self.request(Method::POST, "/orders/status/batch", Some(body)).await?;
        anyhow::ensure!(status == 200 || status == 201, "status batch: {status} {}", short(&reply));
        let item = reply.pointer("/results/0").cloned().unwrap_or(Value::Null);
        if item.get("status").and_then(Value::as_str) != Some("found") {
            return Ok((None, item));
        }
        let execution = item.pointer("/data/execution").cloned().unwrap_or(Value::Null);
        let s = execution.get("settlementStatus").and_then(Value::as_str).map(str::to_owned);
        Ok((s, execution))
    }

    /// Cancel by internal id; returns the round trip (µs) and whether it took.
    pub async fn cancel(&self, order_id: &str) -> anyhow::Result<(u64, bool, Value)> {
        let t0 = Instant::now();
        let body = json!({"orderId": order_id}).to_string();
        let (status, reply) = self.request(Method::POST, "/orders/cancel", Some(body)).await?;
        Ok((t0.elapsed().as_micros() as u64, (200..300).contains(&status), reply))
    }

    /// The dead-man switch: open orders are cancelled at `cancel_at_ms` unless renewed.
    pub async fn heartbeat(&self, cancel_at_ms: i64) -> anyhow::Result<()> {
        let body = json!({"cancelAt": cancel_at_ms}).to_string();
        let (status, reply) = self.request(Method::POST, "/heartbeats", Some(body)).await?;
        anyhow::ensure!((200..300).contains(&status), "heartbeat: {status} {}", short(&reply));
        Ok(())
    }
}

fn short(v: &Value) -> String {
    v.to_string().chars().take(300).collect()
}

/// Base RPC: approvals, balances and redemption from the EOA.
pub struct LimChain {
    provider: DynProvider,
    address: Address,
}

impl LimChain {
    pub async fn connect(rpc_url: &str, private_key: &str) -> anyhow::Result<Self> {
        let signer = PrivateKeySigner::from_str(private_key.trim())?;
        let address = signer.address();
        let provider = ProviderBuilder::new().wallet(signer).connect(rpc_url).await?.erased();
        let chain = provider.get_chain_id().await?;
        anyhow::ensure!(chain == CHAIN_ID, "BASE_RPC_URL is chain {chain}, not Base ({CHAIN_ID})");
        Ok(Self { provider, address })
    }

    pub fn address(&self) -> Address {
        self.address
    }

    async fn call(&self, to: Address, data: Vec<u8>) -> anyhow::Result<alloy::primitives::Bytes> {
        let tx = TxRequest::default().with_to(to).with_input(data);
        Ok(self.provider.call(tx).await?)
    }

    async fn send(&self, to: Address, data: Vec<u8>) -> anyhow::Result<String> {
        let tx = TxRequest::default().with_to(to).with_input(data);
        let receipt = self.provider.send_transaction(tx).await?.get_receipt().await?;
        anyhow::ensure!(receipt.status(), "transaction {} reverted", receipt.transaction_hash);
        Ok(receipt.transaction_hash.to_string())
    }

    /// USDC (6 decimals) and ETH (for gas).
    pub async fn balances(&self) -> anyhow::Result<(f64, f64)> {
        let usdc = Address::from_str(USDC)?;
        let raw = self.call(usdc, balanceOfCall { owner: self.address }.abi_encode()).await?;
        let usdc_bal = balanceOfCall::abi_decode_returns(&raw)?;
        let wei = self.provider.get_balance(self.address).await?;
        Ok((f64::from(usdc_bal) / 1e6, f64::from(wei) / 1e18))
    }

    /// The approvals one venue needs; each is sent only if missing. Returns what was sent.
    pub async fn approve(&self, exchange: Address, fee_module: Option<Address>) -> anyhow::Result<Vec<String>> {
        let usdc = Address::from_str(USDC)?;
        let ctf = Address::from_str(CTF)?;
        let mut sent = Vec::new();
        let mut spenders = vec![("exchange", exchange)];
        if let Some(f) = fee_module {
            spenders.push(("fee_module", f));
        }
        for (name, spender) in spenders {
            let raw = self.call(usdc, allowanceCall { owner: self.address, spender }.abi_encode()).await?;
            if allowanceCall::abi_decode_returns(&raw)? < U256::from(1u64 << 60) {
                let tx = self.send(usdc, approveCall { spender, amount: U256::MAX }.abi_encode()).await?;
                sent.push(format!("USDC → {name} {spender}: {tx}"));
            }
        }
        let raw = self.call(ctf, isApprovedForAllCall { owner: self.address, operator: exchange }.abi_encode()).await?;
        if !isApprovedForAllCall::abi_decode_returns(&raw)? {
            let tx = self.send(ctf, setApprovalForAllCall { operator: exchange, approved: true }.abi_encode()).await?;
            sent.push(format!("CTF → exchange {exchange}: {tx}"));
        }
        Ok(sent)
    }

    /// Both index sets: the same call for a win or a loss.
    pub async fn redeem(&self, condition_id: &str) -> anyhow::Result<String> {
        let call = redeemPositionsCall {
            collateralToken: Address::from_str(USDC)?,
            parentCollectionId: B256::ZERO,
            conditionId: B256::from_str(condition_id)?,
            indexSets: vec![U256::from(1), U256::from(2)],
        };
        self.send(Address::from_str(CTF)?, call.abi_encode()).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn amounts_are_exact() {
        let o = LimOrder {
            slug: "s".into(),
            token_id: "1".into(),
            side: LimSide::Buy,
            price_milli: 483,
            shares: 10,
            tif: LimTif::Fak,
            post_only: false,
        };
        assert_eq!(o.amounts(), (4_830_000, 10_000_000));
        let sell = LimOrder { side: LimSide::Sell, ..o };
        assert_eq!(sell.amounts(), (10_000_000, 4_830_000));
        assert_eq!(price_milli(0.4839, LimSide::Buy), 483);
        assert_eq!(price_milli(0.4831, LimSide::Sell), 484);
        assert_eq!(price_milli(0.483, LimSide::Sell), 483);
    }

    #[test]
    fn hmac_matches_the_documented_construction() {
        // HMAC-SHA256("key", "ts\nGET\n/orders\n") computed independently.
        let h = Hmac::new("t", &STANDARD.encode(b"key")).unwrap();
        let mut mac = HmacImpl::<Sha256>::new_from_slice(b"key").unwrap();
        mac.update(b"2026-01-01T00:00:00.000Z\nGET\n/profiles/me\n");
        let want = STANDARD.encode(mac.finalize().into_bytes());
        assert_eq!(h.signature("2026-01-01T00:00:00.000Z", "GET", "/profiles/me", ""), want);
    }

    #[test]
    fn signed_order_recovers_to_the_signer() {
        // Anvil's first key: a fixed, public test key.
        let s = LimSigner::new("0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80").unwrap();
        let exchange = Address::from_str("0x05c748E2f4DcDe0ec9Fa8DDc40DE6b867f923fa5").unwrap();
        let o = LimOrder {
            slug: "s".into(),
            token_id: "39117137560612644092852533137009893880373413027214242371164796070871332426721".into(),
            side: LimSide::Buy,
            price_milli: 500,
            shares: 10,
            tif: LimTif::Fak,
            post_only: false,
        };
        let v = s.sign(exchange, &o, 7).unwrap();
        assert_eq!(v["makerAmount"], 5_000_000);
        assert_eq!(v["maker"], "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266");
        let sig_hex = v["signature"].as_str().unwrap();
        let sig = alloy::primitives::Signature::from_str(sig_hex).unwrap();
        let order = Order {
            salt: U256::from(7),
            maker: s.address(),
            signer: s.address(),
            taker: Address::ZERO,
            tokenId: U256::from_str(&o.token_id).unwrap(),
            makerAmount: U256::from(5_000_000u64),
            takerAmount: U256::from(10_000_000u64),
            expiration: U256::ZERO,
            nonce: U256::ZERO,
            feeRateBps: U256::ZERO,
            side: 0,
            signatureType: 0,
        };
        let hash = order.eip712_signing_hash(&LimSigner::domain(exchange));
        assert_eq!(sig.recover_address_from_prehash(&hash).unwrap(), s.address());
        assert_eq!(sig_hex.len(), 2 + 130);
    }

    #[test]
    fn totals_from_an_execution() {
        let e = json!({"totalsRaw": {"contractsNet": "10000000", "usdGross": 4830000, "usdFee": "124000"}});
        assert_eq!(filled(&e), Filled { shares: 10.0, usd_gross: 4.83, usd_fee: 0.124 });
    }
}
