//! Turning settled positions back into pUSD.
//!
//! Winning outcome tokens do not pay out by themselves: the holder calls the
//! Conditional Tokens contract's `redeemPositions` once the market resolves.
//! Without that the wallet's balance never recovers, and percentage sizing
//! would shrink after every win. [`CtfRedeemer`] sends that transaction from
//! the EOA (`POLY_SIGNATURE_TYPE=0`; proxy and Safe wallets redeem through
//! Polymarket's relayer, which this service does not use). It pays gas in POL.
//!
//! Both index sets are redeemed, so the call is the same for a win or a loss
//! (a loss burns worthless tokens). The trader checks the token balance
//! afterwards: a redeem that left the shares in place used the wrong
//! collateral and is alerted, never assumed.

use std::future::Future;
use std::str::FromStr;

use alloy::primitives::{Address, B256};
use alloy::providers::{DynProvider, Provider, ProviderBuilder};
use alloy::signers::Signer as _;
use alloy::signers::local::PrivateKeySigner;
use polymarket_client_sdk_v2::POLYGON;
use polymarket_client_sdk_v2::ctf::Client as CtfClient;
use polymarket_client_sdk_v2::ctf::types::RedeemPositionsRequest;
use serde::Serialize;

#[derive(Clone, Debug, Serialize)]
pub struct RedeemReceipt {
    pub condition_id: String,
    pub tx_hash: String,
    pub block: u64,
}

pub trait Redeem: Send + Sync + 'static {
    fn redeem(&self, condition_id: &str) -> impl Future<Output = anyhow::Result<RedeemReceipt>> + Send;
    /// The EOA's POL, for gas.
    fn gas_balance(&self) -> impl Future<Output = anyhow::Result<f64>> + Send;
}

pub struct CtfRedeemer {
    ctf: CtfClient<DynProvider>,
    provider: DynProvider,
    address: Address,
    collateral: Address,
}

impl CtfRedeemer {
    /// `collateral` overrides the exchange's configured collateral (pUSD).
    pub async fn connect(rpc_url: &str, private_key: &str, collateral: Option<&str>) -> anyhow::Result<Self> {
        let signer = PrivateKeySigner::from_str(private_key.trim())?.with_chain_id(Some(POLYGON));
        let address = signer.address();
        let provider = ProviderBuilder::new().wallet(signer).connect(rpc_url).await?.erased();
        let chain = provider.get_chain_id().await?;
        anyhow::ensure!(chain == POLYGON, "POLYGON_RPC_URL is chain {chain}, not Polygon ({POLYGON})");
        let collateral = match collateral {
            Some(c) => Address::from_str(c)?,
            None => {
                polymarket_client_sdk_v2::contract_config(POLYGON, false)
                    .ok_or_else(|| anyhow::anyhow!("no Polygon contract config"))?
                    .collateral
            }
        };
        let ctf = CtfClient::new(provider.clone(), POLYGON)?;
        Ok(Self { ctf, provider, address, collateral })
    }

    pub fn address(&self) -> Address {
        self.address
    }
}

impl Redeem for CtfRedeemer {
    async fn redeem(&self, condition_id: &str) -> anyhow::Result<RedeemReceipt> {
        let condition = B256::from_str(condition_id)?;
        let req = RedeemPositionsRequest::for_binary_market(self.collateral, condition);
        let r = self.ctf.redeem_positions(&req).await?;
        Ok(RedeemReceipt {
            condition_id: condition_id.to_owned(),
            tx_hash: r.transaction_hash.to_string(),
            block: r.block_number,
        })
    }

    async fn gas_balance(&self) -> anyhow::Result<f64> {
        let wei = self.provider.get_balance(self.address).await?;
        Ok(f64::from(wei) / 1e18)
    }
}
