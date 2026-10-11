//! Redeeming from a Polymarket deposit wallet, through Polymarket's relayer.
//!
//! polymarket.com now gives new accounts a deposit wallet: a contract owned by
//! the EOA that holds the pUSD and the outcome tokens (`POLY_SIGNATURE_TYPE=3`,
//! `POLY_FUNDER` = the wallet). The wallet only runs calls that come from its
//! factory, and the factory only from Polymarket's relayer operators, so the
//! EOA cannot send the redeem itself. Instead it signs an EIP-712 `Batch` of
//! calls (domain `DepositWallet` v1, verified by the wallet) and the relayer
//! submits it, paying the gas. Auth is a Relayer API key, created under
//! polymarket.com → Settings → API Keys.
//!
//! The batch is one call: `redeemPositions` on the `CtfCollateralAdapter`,
//! which redeems both index sets of a standard (not negative-risk) CTF market
//! and pays out in pUSD. The 15-minute markets are standard. Ref:
//! docs.polymarket.com/trading/wallets-auth and /trading/positions/manage.

use std::str::FromStr;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use alloy::network::{Ethereum, Network, TransactionBuilder as _};
use alloy::primitives::{Address, B256, Bytes, U256, address, hex};
use alloy::providers::{DynProvider, Provider, ProviderBuilder};
use alloy::signers::SignerSync as _;
use alloy::signers::local::PrivateKeySigner;
use alloy::sol_types::{SolCall, SolStruct, eip712_domain};
use polymarket_client_sdk_v2::POLYGON;
use serde::Deserialize;
use serde_json::{Value, json};

use crate::redeem::{Redeem, RedeemReceipt};

pub const RELAYER_URL: &str = "https://relayer-v2.polymarket.com";
/// The only caller a deposit wallet accepts; the relayer submits through it.
pub const DEPOSIT_WALLET_FACTORY: Address = address!("0x00000000000Fb5C9ADea0298D729A0CB3823Cc07");
/// Redeems a standard CTF market into pUSD for a wallet that approved it.
pub const CTF_COLLATERAL_ADAPTER: Address = address!("0xAdA100Db00Ca00073811820692005400218FcE1f");
/// How long a signed batch stays valid.
const BATCH_TTL: Duration = Duration::from_secs(300);

mod typed {
    alloy::sol! {
        struct Call { address target; uint256 value; bytes data; }
        struct Batch { address wallet; uint256 nonce; uint256 deadline; Call[] calls; }
        function redeemPositions(
            address collateralToken,
            bytes32 parentCollectionId,
            bytes32 conditionId,
            uint256[] indexSets
        );
        function owner() returns (address);
    }
}

#[derive(Clone, Debug)]
pub struct RelayerConfig {
    pub url: String,
    pub api_key: String,
    /// The address that owns the key; the signer's EOA when unset.
    pub api_key_address: Option<String>,
}

pub struct WalletRedeemer {
    http: reqwest::Client,
    url: String,
    api_key: String,
    api_key_address: String,
    signer: PrivateKeySigner,
    wallet: Address,
    collateral: Address,
    provider: Option<DynProvider>,
    poll_every: Duration,
    poll_for: Duration,
}

#[derive(Deserialize)]
struct NonceResp {
    nonce: String,
}

#[derive(Deserialize)]
struct SubmitResp {
    #[serde(rename = "transactionID")]
    transaction_id: String,
}

fn unix_now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or_default()
}

impl WalletRedeemer {
    /// `collateral` overrides the exchange's configured collateral (pUSD).
    pub fn new(
        relayer: RelayerConfig,
        private_key: &str,
        wallet: &str,
        collateral: Option<&str>,
    ) -> anyhow::Result<Self> {
        let signer = PrivateKeySigner::from_str(private_key.trim())?;
        let collateral = match collateral {
            Some(c) => Address::from_str(c)?,
            None => {
                polymarket_client_sdk_v2::contract_config(POLYGON, false)
                    .ok_or_else(|| anyhow::anyhow!("no Polygon contract config"))?
                    .collateral
            }
        };
        let api_key_address = relayer.api_key_address.unwrap_or_else(|| signer.address().to_checksum(None));
        Ok(Self {
            http: reqwest::Client::builder().timeout(Duration::from_secs(15)).build()?,
            url: relayer.url.trim_end_matches('/').to_owned(),
            api_key: relayer.api_key,
            api_key_address,
            signer,
            wallet: Address::from_str(wallet)?,
            collateral,
            provider: None,
            poll_every: Duration::from_secs(2),
            poll_for: Duration::from_secs(120),
        })
    }

    /// Check on chain that the signer owns the wallet, and keep the provider
    /// for the block numbers of receipts.
    pub async fn with_rpc(mut self, rpc_url: &str) -> anyhow::Result<Self> {
        let provider = ProviderBuilder::new().connect(rpc_url).await?.erased();
        let chain = provider.get_chain_id().await?;
        anyhow::ensure!(chain == POLYGON, "POLYGON_RPC_URL is chain {chain}, not Polygon ({POLYGON})");
        let tx = <Ethereum as Network>::TransactionRequest::default()
            .with_to(self.wallet)
            .with_input(typed::ownerCall {}.abi_encode());
        let owner = provider
            .call(tx)
            .await
            .ok()
            .and_then(|r| typed::ownerCall::abi_decode_returns(&r).ok())
            .ok_or_else(|| anyhow::anyhow!("POLY_FUNDER {} is not a deposit wallet (no owner)", self.wallet))?;
        anyhow::ensure!(
            owner == self.signer.address(),
            "POLY_FUNDER {} is owned by {owner}, not this key's {}",
            self.wallet,
            self.signer.address()
        );
        self.provider = Some(provider);
        Ok(self)
    }

    pub fn polling(mut self, every: Duration, total: Duration) -> Self {
        self.poll_every = every;
        self.poll_for = total;
        self
    }

    pub fn wallet(&self) -> Address {
        self.wallet
    }

    fn authed(&self, req: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        req.header("RELAYER_API_KEY", &self.api_key).header("RELAYER_API_KEY_ADDRESS", &self.api_key_address)
    }

    async fn nonce(&self) -> anyhow::Result<U256> {
        let resp = self
            .authed(self.http.get(format!("{}/v1/account/transactions/params", self.url)))
            .query(&[("address", self.signer.address().to_checksum(None).as_str()), ("type", "WALLET")])
            .send()
            .await?;
        let status = resp.status();
        anyhow::ensure!(status.is_success(), "relayer nonce: {status} {}", resp.text().await.unwrap_or_default());
        Ok(U256::from_str(&resp.json::<NonceResp>().await?.nonce)?)
    }

    /// Sign the calls as one batch and hand it to the relayer: its transaction id.
    async fn submit(&self, calls: Vec<typed::Call>, metadata: &str) -> anyhow::Result<String> {
        let nonce = self.nonce().await?;
        let deadline = unix_now() + BATCH_TTL.as_secs();
        let batch = typed::Batch { wallet: self.wallet, nonce, deadline: U256::from(deadline), calls };
        let domain = eip712_domain! {
            name: "DepositWallet",
            version: "1",
            chain_id: POLYGON,
            verifying_contract: self.wallet,
        };
        let sig = self.signer.sign_hash_sync(&batch.eip712_signing_hash(&domain))?;
        let calls: Vec<Value> = batch
            .calls
            .iter()
            .map(|c| json!({"target": c.target.to_checksum(None), "value": c.value.to_string(), "data": c.data}))
            .collect();
        let body = json!({
            "type": "WALLET",
            "from": self.signer.address().to_checksum(None),
            "to": DEPOSIT_WALLET_FACTORY.to_checksum(None),
            "nonce": nonce.to_string(),
            "signature": format!("0x{}", hex::encode(sig.as_bytes())),
            "metadata": metadata,
            "depositWalletParams": {
                "depositWallet": self.wallet.to_checksum(None),
                "deadline": deadline.to_string(),
                "calls": calls,
            },
        });
        let resp = self.authed(self.http.post(format!("{}/submit", self.url))).json(&body).send().await?;
        let status = resp.status();
        anyhow::ensure!(status.is_success(), "relayer submit: {status} {}", resp.text().await.unwrap_or_default());
        Ok(resp.json::<SubmitResp>().await?.transaction_id)
    }

    /// Poll the relayer until the transaction is confirmed: its hash.
    async fn confirmed(&self, id: &str) -> anyhow::Result<String> {
        let t0 = std::time::Instant::now();
        loop {
            tokio::time::sleep(self.poll_every).await;
            let got: anyhow::Result<Value> = async {
                let r = self
                    .authed(self.http.get(format!("{}/v1/account/transactions/{id}", self.url)))
                    .send()
                    .await?
                    .error_for_status()?;
                Ok(r.json().await?)
            }
            .await;
            let last = match got {
                Ok(v) => {
                    let v = if let Value::Array(mut a) = v { a.drain(..).next().unwrap_or(Value::Null) } else { v };
                    let field = |a: &str, b: &str| v.get(a).or_else(|| v.get(b)).and_then(Value::as_str);
                    let state = field("state", "state").unwrap_or_default();
                    match state {
                        "STATE_CONFIRMED" => {
                            return field("transaction_hash", "transactionHash")
                                .map(str::to_owned)
                                .ok_or_else(|| anyhow::anyhow!("relayer transaction {id} confirmed without a hash"));
                        }
                        "STATE_FAILED" | "STATE_INVALID" => anyhow::bail!(
                            "relayer transaction {id} {state}: {}",
                            field("error_msg", "errorMsg").unwrap_or("no error message")
                        ),
                        s => s.to_owned(),
                    }
                }
                Err(e) => format!("{e:#}"),
            };
            anyhow::ensure!(
                t0.elapsed() < self.poll_for,
                "relayer transaction {id} not confirmed after {:?} (last: {last})",
                self.poll_for
            );
        }
    }
}

impl Redeem for WalletRedeemer {
    async fn redeem(&self, condition_id: &str) -> anyhow::Result<RedeemReceipt> {
        let data = typed::redeemPositionsCall {
            collateralToken: self.collateral,
            parentCollectionId: B256::ZERO,
            conditionId: B256::from_str(condition_id)?,
            indexSets: vec![U256::from(1), U256::from(2)],
        }
        .abi_encode();
        let call = typed::Call { target: CTF_COLLATERAL_ADAPTER, value: U256::ZERO, data: Bytes::from(data) };
        let id = self.submit(vec![call], "Redeem position").await?;
        let tx_hash = self.confirmed(&id).await?;
        let block = match &self.provider {
            Some(p) => p
                .get_transaction_receipt(B256::from_str(&tx_hash)?)
                .await
                .ok()
                .flatten()
                .and_then(|r| r.block_number)
                .unwrap_or_default(),
            None => 0,
        };
        Ok(RedeemReceipt { condition_id: condition_id.to_owned(), tx_hash, block })
    }

    /// The relayer pays the gas; the trader does not check it for this wallet.
    async fn gas_balance(&self) -> anyhow::Result<f64> {
        anyhow::bail!("a deposit wallet's transactions are paid by the relayer")
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use axum::Json;
    use axum::extract::{Path, State};
    use axum::http::HeaderMap;
    use axum::routing::{get, post};
    use parking_lot::Mutex;

    use super::*;

    const KEY: &str = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318";
    const WALLET: &str = "0x59316c3d15C292D2c7a9EC206eCB6c6bAAdeAd3e";
    const CONDITION: &str = "0xaabc35349bc01a07a0ad8d863866175165a0640fddd0985e69f94570e0054c42";

    #[derive(Default)]
    struct Relayer {
        submitted: Vec<(HeaderMap, Value)>,
        polls: usize,
        /// The state reported from the second poll on.
        end: &'static str,
    }

    async fn serve(state: Arc<Mutex<Relayer>>) -> String {
        type S = State<Arc<Mutex<Relayer>>>;
        let app = axum::Router::new()
            .route("/v1/account/transactions/params", get(|| async { Json(json!({"address": "0xr", "nonce": "2"})) }))
            .route(
                "/submit",
                post(|State(s): S, h: HeaderMap, Json(b): Json<Value>| async move {
                    s.lock().submitted.push((h, b));
                    Json(json!({"transactionID": "t1", "state": "STATE_NEW"}))
                }),
            )
            .route(
                "/v1/account/transactions/{id}",
                get(|State(s): S, Path(id): Path<String>| async move {
                    let mut s = s.lock();
                    s.polls += 1;
                    let state = if s.polls < 2 { "STATE_NEW" } else { s.end };
                    Json(json!({"transaction_id": id, "transaction_hash": format!("0x{}", "ab".repeat(32)),
                        "state": state, "error_msg": "execution reverted"}))
                }),
            )
            .with_state(state);
        let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap();
        tokio::spawn(async move { axum::serve(l, app).await.unwrap() });
        format!("http://{addr}/")
    }

    fn redeemer(url: String) -> WalletRedeemer {
        let cfg = RelayerConfig { url, api_key: "rk".into(), api_key_address: None };
        WalletRedeemer::new(cfg, KEY, WALLET, None).unwrap().polling(Duration::from_millis(5), Duration::from_secs(5))
    }

    #[test]
    fn batch_type_matches_polymarket() {
        assert_eq!(
            typed::Batch::eip712_encode_type(),
            "Batch(address wallet,uint256 nonce,uint256 deadline,Call[] calls)Call(address target,uint256 value,bytes data)"
        );
        // The Conditional Tokens selector, which the adapter mirrors.
        assert_eq!(typed::redeemPositionsCall::SELECTOR, [0x01, 0xb7, 0x03, 0x7c]);
    }

    #[tokio::test]
    async fn redeems_through_the_relayer_with_a_signed_batch() {
        let state = Arc::new(Mutex::new(Relayer { end: "STATE_CONFIRMED", ..Default::default() }));
        let r = redeemer(serve(state.clone()).await);
        let receipt = r.redeem(CONDITION).await.unwrap();
        assert_eq!(receipt.tx_hash, format!("0x{}", "ab".repeat(32)));
        assert_eq!((receipt.condition_id.as_str(), receipt.block), (CONDITION, 0));

        let s = state.lock();
        let (h, b) = &s.submitted[0];
        let signer = PrivateKeySigner::from_str(KEY).unwrap().address();
        assert_eq!(h["RELAYER_API_KEY"], "rk");
        assert_eq!(h["RELAYER_API_KEY_ADDRESS"], signer.to_checksum(None).as_str());
        assert_eq!((b["type"].as_str(), b["nonce"].as_str()), (Some("WALLET"), Some("2")));
        assert_eq!(b["to"], DEPOSIT_WALLET_FACTORY.to_checksum(None));
        let p = &b["depositWalletParams"];
        assert_eq!(p["depositWallet"], WALLET);
        let call = &p["calls"][0];
        assert_eq!(call["target"], CTF_COLLATERAL_ADAPTER.to_checksum(None));
        let data = hex::decode(call["data"].as_str().unwrap()).unwrap();
        let decoded = typed::redeemPositionsCall::abi_decode(&data).unwrap();
        assert_eq!(decoded.conditionId, B256::from_str(CONDITION).unwrap());
        assert_eq!(decoded.indexSets, vec![U256::from(1), U256::from(2)]);
        assert_eq!(decoded.collateralToken, address!("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"));

        // The signature recovers to the owner over the batch the body describes.
        let batch = typed::Batch {
            wallet: Address::from_str(WALLET).unwrap(),
            nonce: U256::from(2),
            deadline: U256::from_str(p["deadline"].as_str().unwrap()).unwrap(),
            calls: vec![typed::Call { target: CTF_COLLATERAL_ADAPTER, value: U256::ZERO, data: data.into() }],
        };
        let domain = eip712_domain! {
            name: "DepositWallet",
            version: "1",
            chain_id: POLYGON,
            verifying_contract: batch.wallet,
        };
        let sig = alloy::primitives::Signature::from_str(b["signature"].as_str().unwrap()).unwrap();
        assert_eq!(sig.recover_address_from_prehash(&batch.eip712_signing_hash(&domain)).unwrap(), signer);
    }

    #[tokio::test]
    async fn a_failed_relay_is_an_error() {
        let state = Arc::new(Mutex::new(Relayer { end: "STATE_FAILED", ..Default::default() }));
        let r = redeemer(serve(state).await);
        let e = r.redeem(CONDITION).await.unwrap_err().to_string();
        assert!(e.contains("STATE_FAILED") && e.contains("execution reverted"), "{e}");
    }
}
