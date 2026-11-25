//! The live order path: FAK buys at the limit through the trader's own
//! `LiveExec` (signed by Polymarket's SDK, on a warm HTTP/2 connection), and
//! redemption of settled winners (relayer for a deposit wallet, on-chain for
//! an EOA). Reached only from `PK_EG_MODE=live` with `PK_EG_LIVE_CONFIRM=yes`.

use std::sync::Arc;
use std::time::{Duration, Instant};

use pk_exec::presign::PresignCache;
use pk_exec::{
    ClobExec, CtfRedeemer, LiveExec, LiveLimits, RelayerConfig, SdkSigner, WalletConfig, WalletRedeemer, WarmConfig,
    WarmHttp,
};
use rust_decimal::Decimal;
use rust_decimal::prelude::{FromPrimitive, ToPrimitive};

use crate::config::Config;
use crate::engine::LiveResult;

pub enum Redeemer {
    Wallet(WalletRedeemer),
    Eoa(CtfRedeemer),
}

pub struct Exec {
    pub live: LiveExec<SdkSigner>,
    pub clob: Arc<ClobExec<SdkSigner>>,
    pub signature_type: u8,
    redeemer: Redeemer,
}

fn dec(x: f64, dp: u32) -> Decimal {
    Decimal::from_f64(x).unwrap_or_default().round_dp(dp)
}

impl Exec {
    pub async fn connect(cfg: &Config) -> anyhow::Result<Self> {
        let key = cfg.private_key.clone().unwrap_or_default();
        let wallet = WalletConfig {
            clob_url: cfg.clob_url.clone(),
            private_key: key.clone(),
            funder: cfg.funder.clone(),
            signature_type: cfg.signature_type,
        };
        let signer = Arc::new(SdkSigner::connect(&wallet).await?);
        tracing::info!(address = %signer.address(), "endgame order path authenticated");
        let http = WarmHttp::connect(WarmConfig::new(wallet.clob_url.parse()?)).await?;
        http.spawn_maintenance();
        let clob = Arc::new(ClobExec::new(http, signer, PresignCache::new(Duration::from_secs(10), 64)));
        let limits = LiveLimits {
            max_entry_px: dec((cfg.max_px + cfg.limit_offset).min(0.999), 3),
            max_notional: dec(cfg.max_open_cost_usd, 2),
            ..LiveLimits::default()
        };
        let live = LiveExec::new(clob.clone(), limits);
        let rpc = cfg.polygon_rpc_url.clone().unwrap_or_default();
        let collateral = cfg.redeem_collateral.as_deref();
        let redeemer = match (cfg.signature_type == 3, &cfg.funder) {
            (true, Some(funder)) => {
                let relayer = RelayerConfig {
                    url: cfg.relayer_url.clone(),
                    api_key: cfg.relayer_api_key.clone().unwrap_or_default(),
                    api_key_address: cfg.relayer_api_key_address.clone(),
                };
                Redeemer::Wallet(WalletRedeemer::new(relayer, &key, funder, collateral)?.with_rpc(&rpc).await?)
            }
            (true, None) => anyhow::bail!("a deposit wallet needs POLY_FUNDER"),
            (false, _) => Redeemer::Eoa(CtfRedeemer::connect(&rpc, &key, collateral).await?),
        };
        Ok(Self { live, clob, signature_type: cfg.signature_type, redeemer })
    }

    /// A FAK buy of `shares` at up to `limit`. The REST touch gives the tick
    /// and minimum size (the tick narrows to 0.001 near the edges).
    pub async fn enter(&self, token: &str, limit: f64, shares: f64) -> LiveResult {
        let t0 = Instant::now();
        let top = match self.clob.top(token).await {
            Ok(t) => t,
            Err(e) => return LiveResult { error: Some(format!("top: {e:#}")), ..Default::default() },
        };
        let ex = self.live.enter(token, dec(limit, 3), dec(shares, 2), &top, t0).await;
        LiveResult {
            shares: ex.shares,
            usd: ex.usd,
            order_id: ex.order_id,
            error: ex.error.or(ex.refused),
            ack_us: ex.decision_to_ack_us,
        }
    }

    pub async fn redeem(&self, condition_id: &str) -> anyhow::Result<String> {
        let r = match &self.redeemer {
            Redeemer::Wallet(r) => pk_exec::Redeem::redeem(r, condition_id).await?,
            Redeemer::Eoa(r) => pk_exec::Redeem::redeem(r, condition_id).await?,
        };
        Ok(r.tx_hash)
    }

    pub async fn cash(&self) -> anyhow::Result<f64> {
        Ok(self.clob.balance(self.signature_type).await?.to_f64().unwrap_or(0.0))
    }
}
