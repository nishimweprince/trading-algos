//! Turning an order spec into the exact body the CLOB takes.
//!
//! [`SdkSigner`] uses Polymarket's own `polymarket_client_sdk_v2` for the EIP-712
//! order struct, the exchange contract and the signature, so the wire format
//! is theirs, not ours. Only the transport and the timing are ours.

use std::future::Future;
use std::str::FromStr;
use std::time::{Duration, Instant};

use alloy::signers::Signer as _;
use alloy::signers::local::PrivateKeySigner;
use polymarket_client_sdk_v2::POLYGON;
use polymarket_client_sdk_v2::auth::Normal;
use polymarket_client_sdk_v2::auth::state::Authenticated;
use polymarket_client_sdk_v2::clob::types::{OrderType, Side as SdkSide, SignatureType};
use polymarket_client_sdk_v2::clob::{Client, Config};
use polymarket_client_sdk_v2::types::{Address, U256};
use rust_decimal::Decimal;
use secrecy::ExposeSecret as _;
use serde::Serialize;

use crate::auth::L2Creds;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Side {
    Buy,
    Sell,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum Tif {
    /// Rests until cancelled (the probe, with post-only).
    Gtc,
    /// Takes what is there now and cancels the rest (a live entry).
    Fak,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize)]
pub struct OrderSpec {
    pub token: String,
    pub side: Side,
    pub price: Decimal,
    pub size: Decimal,
    pub tif: Tif,
    pub post_only: bool,
}

#[derive(Clone, Debug)]
pub struct SignedBody {
    pub spec: OrderSpec,
    pub body: Vec<u8>,
    pub signed_at: Instant,
    pub sign_time: Duration,
}

pub trait OrderSigner: Send + Sync + 'static {
    fn sign(&self, spec: &OrderSpec) -> impl Future<Output = anyhow::Result<SignedBody>> + Send;
    fn creds(&self) -> &L2Creds;
}

pub struct SdkSigner {
    client: Client<Authenticated<Normal>>,
    signer: PrivateKeySigner,
    creds: L2Creds,
}

#[derive(Clone, Debug)]
pub struct WalletConfig {
    pub clob_url: String,
    pub private_key: String,
    /// The proxy or Safe wallet that holds the funds; None for a plain EOA.
    pub funder: Option<String>,
    /// 0 EOA, 1 Polymarket proxy, 2 Gnosis Safe.
    pub signature_type: u8,
}

impl SdkSigner {
    /// Derive (or create) the L2 API key once, at startup.
    pub async fn connect(cfg: &WalletConfig) -> anyhow::Result<Self> {
        let signer = PrivateKeySigner::from_str(cfg.private_key.trim())?.with_chain_id(Some(POLYGON));
        let mut auth = Client::new(&cfg.clob_url, Config::default())?.authentication_builder(&signer);
        if let Some(funder) = &cfg.funder {
            auth = auth.funder(Address::from_str(funder)?);
        }
        auth = auth.signature_type(match cfg.signature_type {
            0 => SignatureType::Eoa,
            1 => SignatureType::Proxy,
            2 => SignatureType::GnosisSafe,
            other => anyhow::bail!("POLY_SIGNATURE_TYPE {other} is not 0, 1 or 2"),
        });
        let client = auth.authenticate().await?;
        let c = client.credentials();
        let creds = L2Creds::new(
            client.address().to_checksum(None),
            c.key().to_string(),
            c.secret().expose_secret(),
            c.passphrase().expose_secret().to_owned(),
        )?;
        Ok(Self { client, signer, creds })
    }

    pub fn address(&self) -> String {
        self.creds.address.clone()
    }
}

impl OrderSigner for SdkSigner {
    async fn sign(&self, spec: &OrderSpec) -> anyhow::Result<SignedBody> {
        let t0 = Instant::now();
        let signable = self
            .client
            .limit_order()
            .token_id(U256::from_str(&spec.token)?)
            .side(match spec.side {
                Side::Buy => SdkSide::Buy,
                Side::Sell => SdkSide::Sell,
            })
            .price(spec.price)
            .size(spec.size)
            .order_type(match spec.tif {
                Tif::Gtc => OrderType::GTC,
                Tif::Fak => OrderType::FAK,
            })
            .post_only(spec.post_only)
            .build()
            .await?;
        let signed = self.client.sign(&self.signer, signable).await?;
        let body = serde_json::to_vec(&signed)?;
        Ok(SignedBody { spec: spec.clone(), body, signed_at: Instant::now(), sign_time: t0.elapsed() })
    }

    fn creds(&self) -> &L2Creds {
        &self.creds
    }
}
