use std::str::FromStr;

use alloy::network::{Ethereum, Network, TransactionBuilder};
use alloy::primitives::{Address, B256, Bytes, U256};
use alloy::providers::{DynProvider, Provider, ProviderBuilder};
use alloy::signers::local::PrivateKeySigner;
use alloy::sol_types::SolCall;
use serde_json::Value;

use crate::sign::{ChainAddrs, ECDSA_VALIDATOR, chain_addrs};

type TxRequest = <Ethereum as Network>::TransactionRequest;

alloy::sol! {
 function allowance(address owner,address spender) external view returns(uint256);
 function approve(address spender,uint256 amount) external returns(bool);
 function isApprovedForAll(address account,address operator) external view returns(bool);
 function setApprovalForAll(address operator,bool approved) external;
 function balanceOf(address account) external view returns(uint256);
 function ecdsaValidatorStorage(address account) external view returns(address owner);
 function execute(bytes32 mode,bytes executionCalldata) external payable;
 struct ExchangeOrder {
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
    bytes signature;
 }
 function cancelOrders(ExchangeOrder[] orders) external;
 function redeemPositions(address collateralToken,bytes32 parentCollectionId,bytes32 conditionId,uint256[] indexSets) external;
 function mergePositions(address collateralToken,bytes32 parentCollectionId,bytes32 conditionId,uint256[] partition,uint256 amount) external;
}

mod adapter {
    alloy::sol! {
        function redeemPositions(bytes32 conditionId,uint256[] amounts) external;
        function mergePositions(bytes32 conditionId,uint256 amount) external;
    }
}

pub struct PredictChain {
    provider: DynProvider,
    account: Address,
    owner: Address,
    chain_id: u64,
    addrs: ChainAddrs,
}
impl PredictChain {
    pub async fn connect(rpc: &str, key: &str, account: &str, expected: u64) -> anyhow::Result<Self> {
        let addrs = chain_addrs(expected).ok_or_else(|| anyhow::anyhow!("unsupported chain {expected}"))?;
        let signer = PrivateKeySigner::from_str(key.trim())?;
        let owner = signer.address();
        let provider = ProviderBuilder::new().wallet(signer).connect(rpc).await?.erased();
        let chain = provider.get_chain_id().await?;
        anyhow::ensure!(chain == expected, "PREDICT_BNB_RPC_URL is chain {chain}, expected {expected}");
        let this = Self { provider, account: Address::from_str(account)?, owner, chain_id: expected, addrs };
        let raw = this
            .call(ECDSA_VALIDATOR, ecdsaValidatorStorageCall { account: this.account }.abi_encode())
            .await?;
        let configured = ecdsaValidatorStorageCall::abi_decode_returns(&raw)?;
        anyhow::ensure!(configured == owner, "Privy signer is not the smart account's ECDSA validator owner");
        Ok(this)
    }
    async fn call(&self, to: Address, data: Vec<u8>) -> anyhow::Result<Bytes> {
        Ok(self.provider.call(TxRequest::default().with_to(to).with_input(data)).await?)
    }
    async fn kernel_send(&self, target: Address, data: Vec<u8>) -> anyhow::Result<String> {
        let mut execution = Vec::with_capacity(52 + data.len());
        execution.extend_from_slice(target.as_slice());
        execution.extend_from_slice(&[0_u8; 32]);
        execution.extend_from_slice(&data);
        let call = executeCall { mode: [0_u8; 32].into(), executionCalldata: execution.into() };
        let receipt = self
            .provider
            .send_transaction(TxRequest::default().with_to(self.account).with_input(call.abi_encode()))
            .await?
            .get_receipt()
            .await?;
        anyhow::ensure!(receipt.status(), "smart-account transaction reverted");
        Ok(receipt.transaction_hash.to_string())
    }
    pub async fn balances(&self) -> anyhow::Result<(f64, f64)> {
        let raw = self.call(self.addrs.usdt, balanceOfCall { account: self.account }.abi_encode()).await?;
        let usdt = balanceOfCall::abi_decode_returns(&raw)?;
        let gas = self.provider.get_balance(self.owner).await?;
        Ok((u256_f64(usdt) / 1e18, u256_f64(gas) / 1e18))
    }
    pub async fn approve_all(&self) -> anyhow::Result<Vec<String>> {
        let a = self.addrs;
        let mut sent = vec![];
        for (token, operator, label) in [
            (a.ctf, a.standard_exchange, "ctf-standard"),
            (a.ctf, a.standard_neg_exchange, "ctf-neg-risk"),
            (a.ctf, a.neg_adapter, "ctf-neg-adapter"),
            (a.yield_ctf, a.yield_exchange, "yield-ctf"),
            (a.yield_neg_ctf, a.yield_neg_exchange, "yield-neg-ctf"),
            (a.yield_neg_ctf, a.yield_neg_adapter, "yield-neg-adapter"),
        ] {
            let raw = self
                .call(token, isApprovedForAllCall { account: self.account, operator }.abi_encode())
                .await?;
            if !isApprovedForAllCall::abi_decode_returns(&raw)? {
                let hash = self
                    .kernel_send(token, setApprovalForAllCall { operator, approved: true }.abi_encode())
                    .await?;
                sent.push(format!("{label}: {hash}"));
            }
        }
        for (spender, label) in [
            (a.standard_exchange, "usdt-standard"),
            (a.standard_neg_exchange, "usdt-neg-risk"),
            (a.yield_exchange, "usdt-yield"),
            (a.yield_neg_exchange, "usdt-yield-neg-risk"),
        ] {
            let raw = self.call(a.usdt, allowanceCall { owner: self.account, spender }.abi_encode()).await?;
            if allowanceCall::abi_decode_returns(&raw)? < U256::from(1_u128 << 120) {
                let hash =
                    self.kernel_send(a.usdt, approveCall { spender, amount: U256::MAX }.abi_encode()).await?;
                sent.push(format!("{label}: {hash}"));
            }
        }
        Ok(sent)
    }

    pub async fn cancel_orders(
        &self,
        orders: &[Value],
        is_yield_bearing: bool,
        is_neg_risk: bool,
    ) -> anyhow::Result<Option<String>> {
        if orders.is_empty() {
            return Ok(None);
        }
        let orders = orders.iter().map(parse_order).collect::<anyhow::Result<Vec<_>>>()?;
        let target = crate::sign::exchange_for(is_yield_bearing, is_neg_risk, self.chain_id);
        Ok(Some(self.kernel_send(target, cancelOrdersCall { orders }.abi_encode()).await?))
    }

    pub async fn redeem_positions(
        &self,
        condition_id: B256,
        index_set: u8,
        amount: U256,
        is_yield_bearing: bool,
        is_neg_risk: bool,
    ) -> anyhow::Result<String> {
        anyhow::ensure!(index_set == 1 || index_set == 2, "index set is 1 (Yes) or 2 (No)");
        let (target, data) =
            redeem_calldata(self.addrs, condition_id, index_set, amount, is_yield_bearing, is_neg_risk);
        self.kernel_send(target, data).await
    }

    pub async fn merge_positions(
        &self,
        condition_id: B256,
        amount: U256,
        is_yield_bearing: bool,
        is_neg_risk: bool,
    ) -> anyhow::Result<String> {
        anyhow::ensure!(amount > U256::ZERO, "merge amount must be positive");
        let (target, data) = merge_calldata(self.addrs, condition_id, amount, is_yield_bearing, is_neg_risk);
        self.kernel_send(target, data).await
    }
}

/// Pure calldata routing so the settlement loop can be tested without an RPC.
fn redeem_calldata(
    addrs: ChainAddrs,
    condition_id: B256,
    index_set: u8,
    amount: U256,
    is_yield_bearing: bool,
    is_neg_risk: bool,
) -> (Address, Vec<u8>) {
    if is_neg_risk {
        let amounts = if index_set == 1 { vec![amount, U256::ZERO] } else { vec![U256::ZERO, amount] };
        let target = if is_yield_bearing { addrs.yield_neg_adapter } else { addrs.neg_adapter };
        (target, adapter::redeemPositionsCall { conditionId: condition_id, amounts }.abi_encode())
    } else {
        let target = if is_yield_bearing { addrs.yield_ctf } else { addrs.ctf };
        (
            target,
            redeemPositionsCall {
                collateralToken: addrs.usdt,
                parentCollectionId: B256::ZERO,
                conditionId: condition_id,
                indexSets: vec![U256::from(index_set)],
            }
            .abi_encode(),
        )
    }
}

fn merge_calldata(
    addrs: ChainAddrs,
    condition_id: B256,
    amount: U256,
    is_yield_bearing: bool,
    is_neg_risk: bool,
) -> (Address, Vec<u8>) {
    if is_neg_risk {
        let target = if is_yield_bearing { addrs.yield_neg_adapter } else { addrs.neg_adapter };
        (target, adapter::mergePositionsCall { conditionId: condition_id, amount }.abi_encode())
    } else {
        let target = if is_yield_bearing { addrs.yield_ctf } else { addrs.ctf };
        (
            target,
            mergePositionsCall {
                collateralToken: addrs.usdt,
                parentCollectionId: B256::ZERO,
                conditionId: condition_id,
                partition: vec![U256::from(1), U256::from(2)],
                amount,
            }
            .abi_encode(),
        )
    }
}

fn parse_order(value: &Value) -> anyhow::Result<ExchangeOrder> {
    let value = value.get("order").unwrap_or(value);
    let number = |key: &str| -> anyhow::Result<U256> {
        let value = value.get(key).ok_or_else(|| anyhow::anyhow!("cancel order missing {key}"))?;
        let raw = value.as_str().map(str::to_owned).unwrap_or_else(|| value.to_string());
        Ok(U256::from_str(&raw)?)
    };
    let address = |key: &str| -> anyhow::Result<Address> {
        Address::from_str(
            value
                .get(key)
                .and_then(Value::as_str)
                .ok_or_else(|| anyhow::anyhow!("cancel order missing {key}"))?,
        )
        .map_err(Into::into)
    };
    Ok(ExchangeOrder {
        salt: number("salt")?,
        maker: address("maker")?,
        signer: address("signer")?,
        taker: address("taker")?,
        tokenId: number("tokenId")?,
        makerAmount: number("makerAmount")?,
        takerAmount: number("takerAmount")?,
        expiration: number("expiration")?,
        nonce: number("nonce")?,
        feeRateBps: number("feeRateBps")?,
        side: number("side")?.to::<u8>(),
        signatureType: number("signatureType")?.to::<u8>(),
        signature: Bytes::new(),
    })
}
fn u256_f64(v: U256) -> f64 {
    v.to_string().parse().unwrap_or(0.0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn cancellation_order_uses_exact_api_integers_and_empty_signature() {
        let order = parse_order(&json!({
            "salt":"7", "maker":"0x0000000000000000000000000000000000000001",
            "signer":"0x0000000000000000000000000000000000000001",
            "taker":"0x0000000000000000000000000000000000000000",
            "tokenId":"9", "makerAmount":"550000000000000000",
            "takerAmount":"1000000000000000000", "expiration":"123",
            "nonce":"0", "feeRateBps":"100", "side":0, "signatureType":0,
            "signature":"must-not-be-forwarded"
        }))
        .unwrap();
        assert_eq!(order.makerAmount, U256::from(550_000_000_000_000_000_u64));
        assert!(order.signature.is_empty());
        assert!(!cancelOrdersCall { orders: vec![order] }.abi_encode().is_empty());
    }

    fn selector(signature: &str) -> [u8; 4] {
        alloy::primitives::keccak256(signature.as_bytes())[..4].try_into().unwrap()
    }

    #[test]
    fn redeem_routes_to_ctf_or_adapter_with_sdk_selectors() {
        use crate::sign::{BNB_MAINNET, chain_addrs};
        let addrs = chain_addrs(BNB_MAINNET).unwrap();
        let condition =
            B256::from_str("0x1111111111111111111111111111111111111111111111111111111111111111").unwrap();
        let (target, data) = redeem_calldata(addrs, condition, 1, U256::from(5), false, false);
        assert_eq!(target, addrs.ctf);
        assert_eq!(&data[..4], selector("redeemPositions(address,bytes32,bytes32,uint256[])"));
        let (target, data) = redeem_calldata(addrs, condition, 2, U256::from(5), true, true);
        assert_eq!(target, addrs.yield_neg_adapter);
        assert_eq!(&data[..4], selector("redeemPositions(bytes32,uint256[])"));
        // Neg-risk index set 2 redeems [0, amount].
        let tail = &data[4..];
        assert!(tail.windows(32).any(|w| w == U256::from(5).to_be_bytes::<32>()));
    }

    #[test]
    fn merge_routes_to_ctf_or_adapter_with_sdk_selectors() {
        use crate::sign::{BNB_MAINNET, chain_addrs};
        let addrs = chain_addrs(BNB_MAINNET).unwrap();
        let condition =
            B256::from_str("0x1111111111111111111111111111111111111111111111111111111111111111").unwrap();
        let (target, data) = merge_calldata(addrs, condition, U256::from(3), true, false);
        assert_eq!(target, addrs.yield_ctf);
        assert_eq!(&data[..4], selector("mergePositions(address,bytes32,bytes32,uint256[],uint256)"));
        let (target, data) = merge_calldata(addrs, condition, U256::from(3), false, true);
        assert_eq!(target, addrs.neg_adapter);
        assert_eq!(&data[..4], selector("mergePositions(bytes32,uint256)"));
    }
}
