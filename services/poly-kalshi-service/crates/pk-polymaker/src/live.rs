//! The live order path: post-only GTC buys and cancels on Polymarket's CLOB
//! (the same signer, warm connection and redeemers as the trader), the order
//! poll that turns `size_matched` into fills, and the wallet checks.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use pk_exec::presign::PresignCache;
use pk_exec::{ClobExec, CtfRedeemer, OrderSpec, RelayerConfig, SdkSigner, Side, Tif, Trigger, WalletConfig, WalletRedeemer, WarmConfig, WarmHttp};
use pk_svc::Settings;
use rust_decimal::Decimal;
use rust_decimal::prelude::{FromPrimitive, ToPrimitive};
use serde_json::Value;

pub fn now_ns() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos() as i64).unwrap_or(0)
}

pub enum Redeemer {
    /// A deposit wallet: the relayer submits and pays the gas.
    Wallet(WalletRedeemer),
    Eoa(CtfRedeemer),
}

impl Redeemer {
    pub async fn redeem(&self, condition_id: &str) -> anyhow::Result<String> {
        let r = match self {
            Redeemer::Wallet(r) => pk_exec::Redeem::redeem(r, condition_id).await?,
            Redeemer::Eoa(r) => pk_exec::Redeem::redeem(r, condition_id).await?,
        };
        Ok(r.tx_hash)
    }
}

pub struct Exec {
    pub clob: Arc<ClobExec<SdkSigner>>,
    pub signature_type: u8,
    pub redeemer: Redeemer,
}

fn dec(x: f64, dp: u32) -> Decimal {
    Decimal::from_f64(x).unwrap_or_default().round_dp(dp)
}

fn num(v: &Value) -> Option<f64> {
    v.as_f64().or_else(|| v.as_str().and_then(|s| s.parse().ok()))
}

impl Exec {
    pub async fn connect(settings: &Settings) -> anyhow::Result<Self> {
        let wallet = WalletConfig {
            clob_url: settings.poly_clob_url.clone(),
            private_key: settings.poly_private_key.clone().unwrap_or_default(),
            funder: settings.poly_funder.clone(),
            signature_type: settings.poly_signature_type,
        };
        let signer = Arc::new(SdkSigner::connect(&wallet).await?);
        tracing::info!(address = %signer.address(), "pm order path authenticated");
        let http = WarmHttp::connect(WarmConfig::new(wallet.clob_url.parse()?)).await?;
        http.spawn_maintenance();
        let clob = Arc::new(ClobExec::new(http, signer, PresignCache::new(Duration::from_secs(10), 64)));
        let rpc = settings.polygon_rpc_url.clone().unwrap_or_default();
        let collateral = settings.poly_redeem_collateral.as_deref();
        let redeemer = match (settings.poly_signature_type == 3, &wallet.funder) {
            (true, Some(funder)) => {
                let relayer = RelayerConfig {
                    url: settings.poly_relayer_url.clone(),
                    api_key: settings.poly_relayer_api_key.clone().unwrap_or_default(),
                    api_key_address: settings.poly_relayer_api_key_address.clone(),
                };
                let r = WalletRedeemer::new(relayer, &wallet.private_key, funder, collateral)?.with_rpc(&rpc).await?;
                tracing::info!(wallet = %r.wallet(), "pm redeemer connected (deposit wallet, relayer)");
                Redeemer::Wallet(r)
            }
            (true, None) => anyhow::bail!("a deposit wallet redeemer needs POLY_FUNDER"),
            (false, _) => {
                let r = CtfRedeemer::connect(&rpc, &wallet.private_key, collateral).await?;
                tracing::info!(address = %r.address(), "pm redeemer connected");
                Redeemer::Eoa(r)
            }
        };
        Ok(Self { clob, signature_type: settings.poly_signature_type, redeemer })
    }

    /// A post-only GTC buy: the order id, or why not.
    pub async fn post(&self, token: &str, px: f64, size: f64) -> (Result<String, String>, u64) {
        let spec = OrderSpec {
            token: token.to_owned(),
            side: Side::Buy,
            price: dec(px, 2),
            size: dec(size, 2),
            tif: Tif::Gtc,
            post_only: true,
        };
        let (ack, timing) = self.clob.post(&spec, Instant::now(), Trigger::Decision).await;
        let us = timing.decision_to_ack_us;
        match ack {
            Some(a) if a.success && !a.order_id.is_empty() => (Ok(a.order_id), us),
            Some(a) => (Err(a.error_msg.or(timing.error).unwrap_or_else(|| "rejected".into())), us),
            None => (Err(timing.error.unwrap_or_else(|| format!("http {}", timing.status))), us),
        }
    }

    pub async fn cancel(&self, order_id: &str) -> (bool, Option<String>) {
        let (ack, timing) = self.clob.cancel(order_id, Trigger::Cadence).await;
        if timing.ok {
            return (true, None);
        }
        let why = ack
            .and_then(|a| a.not_canceled.get(order_id).map(|v| v.to_string()))
            .or(timing.error)
            .unwrap_or_else(|| format!("http {}", timing.status));
        (false, Some(why))
    }

    /// Matched shares of each order resting on the account.
    pub async fn open_matched(&self) -> anyhow::Result<HashMap<String, f64>> {
        let orders = self.clob.open_orders().await?;
        Ok(orders
            .iter()
            .filter_map(|o| Some((o.get("id")?.as_str()?.to_owned(), o.get("size_matched").and_then(num).unwrap_or(0.0))))
            .collect())
    }

    /// One order's state: (matched shares, still resting), or None while the
    /// venue answers `null` (not indexed yet, or long gone).
    pub async fn final_state(&self, order_id: &str) -> anyhow::Result<Option<(f64, bool)>> {
        let v = self.clob.order_value(order_id).await?;
        if v.is_null() {
            return Ok(None);
        }
        let o = v.get("order").unwrap_or(&v);
        let status = o.get("status").and_then(Value::as_str).unwrap_or_default();
        anyhow::ensure!(!status.is_empty(), "no status in {}", &v.to_string()[..v.to_string().len().min(200)]);
        let matched = o.get("size_matched").and_then(num).unwrap_or(0.0);
        Ok(Some((matched, status.eq_ignore_ascii_case("live"))))
    }

    pub async fn token_balance(&self, token: &str) -> anyhow::Result<f64> {
        Ok(self.clob.token_balance(token, self.signature_type).await?.to_f64().unwrap_or(0.0))
    }

    pub async fn cash(&self) -> anyhow::Result<f64> {
        Ok(self.clob.balance(self.signature_type).await?.to_f64().unwrap_or(0.0))
    }
}
