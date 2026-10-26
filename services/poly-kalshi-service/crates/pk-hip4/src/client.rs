use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use tokio::sync::{mpsc, oneshot};
use tokio_tungstenite::tungstenite::Message;

use crate::sign::{HlSigner, Node};

pub struct Client {
    pub http: reqwest::Client,
    pub api_url: String,
    pub ws_url: String,
    pub account: String,
    signer: Option<Arc<HlSigner>>,
    nonce: AtomicU64,
}
impl Client {
    pub fn new(api: &str, ws: &str, account: Option<&str>, key: Option<&str>) -> anyhow::Result<Self> {
        Ok(Self {
            http: reqwest::Client::builder()
                .connect_timeout(std::time::Duration::from_secs(3))
                .timeout(std::time::Duration::from_secs(8))
                .build()?,
            api_url: api.trim_end_matches('/').into(),
            ws_url: ws.into(),
            account: account.unwrap_or_default().into(),
            signer: key.map(HlSigner::new).transpose()?.map(Arc::new),
            nonce: AtomicU64::new(0),
        })
    }
    pub async fn info(&self, body: Value) -> anyhow::Result<Value> {
        let r = self.http.post(format!("{}/info", self.api_url)).json(&body).send().await?;
        let status = r.status();
        let value = r.json::<Value>().await.unwrap_or_default();
        anyhow::ensure!(status.is_success(), "Hyperliquid info HTTP {status}: {}", safe_error(&value));
        Ok(value)
    }
    pub async fn outcome_meta(&self) -> anyhow::Result<Value> {
        self.info(json!({"type":"outcomeMeta"})).await
    }
    pub async fn outcome_templates(&self) -> anyhow::Result<Value> {
        self.info(json!({"type":"outcomeTemplates"})).await
    }
    pub async fn open_orders(&self) -> anyhow::Result<Value> {
        self.info(json!({"type":"openOrders","user":self.account})).await
    }
    pub async fn user_state(&self) -> anyhow::Result<Value> {
        self.info(json!({"type":"clearinghouseState","user":self.account})).await
    }
    pub fn next_nonce(&self) -> u64 {
        let now = pk_venue_core::service::now_ms().max(0) as u64;
        loop {
            let prev = self.nonce.load(Ordering::Relaxed);
            let next = now.max(prev + 1);
            if self.nonce.compare_exchange(prev, next, Ordering::SeqCst, Ordering::Relaxed).is_ok() {
                return next;
            }
        }
    }
    pub fn signed(&self, action: &Node, expiry_ms: u64) -> anyhow::Result<Value> {
        let signer =
            self.signer.as_ref().ok_or_else(|| anyhow::anyhow!("HL_AGENT_PRIVATE_KEY is not configured"))?;
        let nonce = self.next_nonce();
        let expiry = if expiry_ms > 0 { nonce + expiry_ms } else { 0 };
        signer.payload(action, nonce, expiry)
    }
    pub async fn exchange_rest(&self, payload: &Value) -> anyhow::Result<Value> {
        let r = self.http.post(format!("{}/exchange", self.api_url)).json(payload).send().await?;
        let status = r.status();
        let value = r.json::<Value>().await.unwrap_or_default();
        anyhow::ensure!(status.is_success(), "Hyperliquid exchange HTTP {status}: {}", safe_error(&value));
        Ok(value)
    }
    pub fn signer_address(&self) -> Option<String> {
        self.signer.as_ref().map(|s| s.address().to_checksum(None))
    }
}

fn safe_error(v: &Value) -> String {
    v.get("response")
        .or_else(|| v.get("error"))
        .or_else(|| v.get("status"))
        .map(Value::to_string)
        .unwrap_or_else(|| "request rejected".into())
        .chars()
        .take(200)
        .collect()
}

struct Command {
    action: Node,
    expiry: u64,
    reply: oneshot::Sender<anyhow::Result<Value>>,
}
#[derive(Clone)]
pub struct Gateway {
    tx: mpsc::Sender<Command>,
}
impl Gateway {
    pub fn spawn(client: Arc<Client>) -> Self {
        let (tx, rx) = mpsc::channel(1024);
        tokio::spawn(run_gateway(client, rx));
        Self { tx }
    }
    pub async fn submit(&self, action: Node, expiry: u64) -> anyhow::Result<Value> {
        let (tx, rx) = oneshot::channel();
        self.tx
            .send(Command { action, expiry, reply: tx })
            .await
            .map_err(|_| anyhow::anyhow!("gateway stopped"))?;
        rx.await.map_err(|_| anyhow::anyhow!("gateway response dropped"))?
    }
}

async fn run_gateway(client: Arc<Client>, mut rx: mpsc::Receiver<Command>) {
    loop {
        match tokio_tungstenite::connect_async(&client.ws_url).await {
            Ok((mut ws, _)) => {
                let _=ws.send(Message::Text(json!({"method":"subscribe","subscription":{"type":"orderUpdates","user":client.account}}).to_string().into())).await;
                let _=ws.send(Message::Text(json!({"method":"subscribe","subscription":{"type":"userFills","user":client.account}}).to_string().into())).await;
                let mut id = 1_u64;
                loop {
                    tokio::select! {
                        command=rx.recv()=>{let Some(command)=command else{return};let payload=match client.signed(&command.action,command.expiry){Ok(v)=>v,Err(e)=>{let _=command.reply.send(Err(e));continue}};let message=json!({"method":"post","id":id,"request":{"type":"action","payload":payload}});let started=id;id+=1;if ws.send(Message::Text(message.to_string().into())).await.is_err(){let _=command.reply.send(client.exchange_rest(&payload).await);break}let result=tokio::time::timeout(std::time::Duration::from_secs(2),async{loop{match ws.next().await{Some(Ok(frame)) if frame.is_text()=>{let v:Value=serde_json::from_str(frame.to_text().unwrap_or("{}"))?;if v.pointer("/data/id").and_then(Value::as_u64)==Some(started){return Ok(v)}},Some(Ok(frame)) if frame.is_ping()=>{ws.send(Message::Pong(frame.into_data())).await?},Some(Err(e))=>return Err(anyhow::anyhow!(e)),None=>return Err(anyhow::anyhow!("WebSocket closed")),_=>{}}}}).await;match result{Ok(Ok(v))=>{let _=command.reply.send(Ok(v));},_=>{let _=command.reply.send(client.exchange_rest(&payload).await);break}}}
                        frame=ws.next()=>{match frame{Some(Ok(frame)) if frame.is_ping()=>{let _=ws.send(Message::Pong(frame.into_data())).await;},Some(Err(_))|None=>break,_=>{}}}
                    }
                }
            }
            Err(_) => {
                if let Some(command) = rx.recv().await {
                    let result = match client.signed(&command.action, command.expiry) {
                        Ok(payload) => client.exchange_rest(&payload).await,
                        Err(error) => Err(error),
                    };
                    let _ = command.reply.send(result);
                }
            }
        }
        tokio::time::sleep(std::time::Duration::from_secs(1)).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sign::OrderSpec;
    use crate::sign::{cancel_by_cloid, order_action};
    use axum::Router;
    use axum::extract::State;
    use axum::routing::post;
    use serde_json::json;

    const KEY: &str = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318";

    #[derive(Clone, Default)]
    struct Mock {
        seen: Arc<parking_lot::Mutex<Vec<Value>>>,
    }

    async fn serve_rest(mock: Mock) -> String {
        async fn info(State(mock): State<Mock>, body: axum::Json<Value>) -> axum::Json<Value> {
            mock.seen.lock().push(body.0.clone());
            if body.get("type").and_then(Value::as_str) == Some("explode") {
                return axum::Json(json!({"not": "an array"}));
            }
            axum::Json(json!({"ok": true, "echo": body.0}))
        }
        async fn exchange(State(mock): State<Mock>, body: axum::Json<Value>) -> axum::Json<Value> {
            mock.seen.lock().push(body.0);
            axum::Json(json!({"status": "ok", "via": "rest"}))
        }
        let app =
            Router::new().route("/info", post(info)).route("/exchange", post(exchange)).with_state(mock);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://127.0.0.1:{}", listener.local_addr().unwrap().port());
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        base
    }

    /// Mock exchange WebSocket: answers every `post` with the matching id.
    async fn serve_ws() -> String {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("ws://127.0.0.1:{}/ws", listener.local_addr().unwrap().port());
        tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                tokio::spawn(async move {
                    let mut ws = tokio_tungstenite::accept_async(stream).await.unwrap();
                    while let Some(Ok(frame)) = ws.next().await {
                        if !frame.is_text() {
                            continue;
                        }
                        let value: Value = serde_json::from_str(frame.to_text().unwrap()).unwrap();
                        if value.get("method").and_then(Value::as_str) != Some("post") {
                            continue;
                        }
                        let id = value.get("id").cloned().unwrap_or(Value::Null);
                        let reply = json!({"channel":"post","data":{"id":id,"response":{"status":"ok"}}});
                        if ws.send(Message::Text(reply.to_string().into())).await.is_err() {
                            break;
                        }
                    }
                });
            }
        });
        url
    }

    fn order() -> Node {
        order_action(&[OrderSpec {
            asset: 100175880,
            buy: true,
            price: "0.42".into(),
            size: "1".into(),
            reduce_only: false,
            tif: "Alo",
            cloid: Some("0x00000000000000000000000000000009".into()),
        }])
    }

    #[tokio::test]
    async fn info_posts_exact_body() {
        let mock = Mock::default();
        let base = serve_rest(mock.clone()).await;
        let client = Client::new(&base, "ws://127.0.0.1:9/ws", Some("0xabc"), None).unwrap();
        let reply = client.info(json!({"type": "outcomeMeta"})).await.unwrap();
        assert_eq!(reply["ok"], true);
        assert_eq!(mock.seen.lock().last().unwrap()["type"], "outcomeMeta");
    }

    #[tokio::test]
    async fn nonces_are_strictly_monotonic_across_tasks() {
        let client = Arc::new(Client::new("http://127.0.0.1:9", "ws://127.0.0.1:9/ws", None, None).unwrap());
        let mut handles = Vec::new();
        for _ in 0..8 {
            let client = client.clone();
            handles
                .push(tokio::spawn(async move { (0..50).map(|_| client.next_nonce()).collect::<Vec<_>>() }));
        }
        let mut all = Vec::new();
        for handle in handles {
            all.extend(handle.await.unwrap());
        }
        let mut sorted = all.clone();
        sorted.sort_unstable();
        sorted.dedup();
        assert_eq!(all.len(), 400);
        assert_eq!(sorted.len(), 400, "every nonce is unique");
        assert!(all.windows(2).all(|w| w[0] != w[1]));
    }

    #[tokio::test]
    async fn signed_payload_needs_a_key_and_carries_expiry() {
        let anon = Client::new("http://127.0.0.1:9", "ws://127.0.0.1:9/ws", None, None).unwrap();
        assert!(anon.signed(&order(), 0).is_err());
        let authed =
            Client::new("http://127.0.0.1:9", "ws://127.0.0.1:9/ws", Some("0xabc"), Some(KEY)).unwrap();
        let payload = authed.signed(&order(), 5000).unwrap();
        assert!(payload["signature"]["r"].as_str().unwrap().starts_with("0x"));
        assert!(payload["expiresAfter"].as_u64().unwrap() > payload["nonce"].as_u64().unwrap());
        let plain = authed.signed(&cancel_by_cloid(7, "0x01"), 0).unwrap();
        assert!(plain.get("expiresAfter").is_none());
    }

    #[tokio::test]
    async fn gateway_matches_ws_replies_by_id() {
        let mock = Mock::default();
        let rest = serve_rest(mock.clone()).await;
        let ws = serve_ws().await;
        let client = Arc::new(Client::new(&rest, &ws, Some("0xabc"), Some(KEY)).unwrap());
        let gateway = Gateway::spawn(client);
        let reply = gateway.submit(order(), 0).await.unwrap();
        assert_eq!(reply["data"]["response"]["status"], "ok");
        assert_eq!(reply["data"]["id"], 1);
        // Nothing fell through to REST while the socket answered.
        assert!(mock.seen.lock().is_empty());
    }

    #[tokio::test]
    async fn gateway_falls_back_to_rest_when_ws_is_down() {
        let mock = Mock::default();
        let rest = serve_rest(mock.clone()).await;
        // Nothing listens on this port: connect fails and REST carries the action.
        let client = Arc::new(Client::new(&rest, "ws://127.0.0.1:9/ws", Some("0xabc"), Some(KEY)).unwrap());
        let gateway = Gateway::spawn(client);
        let reply = gateway.submit(order(), 0).await.unwrap();
        assert_eq!(reply["via"], "rest");
        let sent = mock.seen.lock().last().cloned().unwrap();
        assert_eq!(sent["action"]["type"], "order");
        assert!(sent["nonce"].as_u64().unwrap() > 0);
    }
}
