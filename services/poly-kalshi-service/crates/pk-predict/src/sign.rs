use std::str::FromStr;

use alloy::primitives::{Address, B256, U256, keccak256};
use alloy::signers::SignerSync as _;
use alloy::signers::local::PrivateKeySigner;
use alloy::sol_types::{Eip712Domain, SolStruct};
use serde_json::{Value, json};

pub const BNB_MAINNET: u64 = 56;
pub const BNB_TESTNET: u64 = 97;
pub const ECDSA_VALIDATOR: Address = alloy::primitives::address!("845ADb2C711129d4f3966735eD98a9F09fC4cE57");
pub const YIELD_EXCHANGE: Address = alloy::primitives::address!("6bEb5a40C032AFc305961162d8204CDA16DECFa5");
pub const YIELD_NEG_RISK_EXCHANGE: Address =
    alloy::primitives::address!("8A289d458f5a134bA40015085A8F50Ffb681B41d");
pub const STANDARD_EXCHANGE: Address =
    alloy::primitives::address!("8BC070BEdAB741406F4B1Eb65A72bee27894B689");
pub const STANDARD_NEG_RISK_EXCHANGE: Address =
    alloy::primitives::address!("365fb81bd4A24D6303cd2F19c349dE6894D8d58A");

/// Deployed protocol addresses per chain, mirroring the official SDK's
/// `AddressesByChainId`. The ECDSA validator is the same on both chains.
#[derive(Clone, Copy, Debug)]
pub struct ChainAddrs {
    pub usdt: Address,
    pub ctf: Address,
    pub yield_ctf: Address,
    pub yield_neg_ctf: Address,
    pub neg_adapter: Address,
    pub yield_neg_adapter: Address,
    pub yield_exchange: Address,
    pub yield_neg_exchange: Address,
    pub standard_exchange: Address,
    pub standard_neg_exchange: Address,
}

pub fn chain_addrs(chain_id: u64) -> Option<ChainAddrs> {
    match chain_id {
        BNB_MAINNET => Some(ChainAddrs {
            usdt: alloy::primitives::address!("55d398326f99059fF775485246999027B3197955"),
            ctf: alloy::primitives::address!("22DA1810B194ca018378464a58f6Ac2B10C9d244"),
            yield_ctf: alloy::primitives::address!("9400F8Ad57e9e0F352345935d6D3175975eb1d9F"),
            yield_neg_ctf: alloy::primitives::address!("F64b0b318AAf83BD9071110af24D24445719A07F"),
            neg_adapter: alloy::primitives::address!("c3Cf7c252f65E0d8D88537dF96569AE94a7F1A6E"),
            yield_neg_adapter: alloy::primitives::address!("41dCe1A4B8FB5e6327701750aF6231B7CD0B2A40"),
            yield_exchange: YIELD_EXCHANGE,
            yield_neg_exchange: YIELD_NEG_RISK_EXCHANGE,
            standard_exchange: STANDARD_EXCHANGE,
            standard_neg_exchange: STANDARD_NEG_RISK_EXCHANGE,
        }),
        BNB_TESTNET => Some(ChainAddrs {
            usdt: alloy::primitives::address!("B32171ecD878607FFc4F8FC0bCcE6852BB3149E0"),
            ctf: alloy::primitives::address!("2827AAef52D71910E8FBad2FfeBC1B6C2DA37743"),
            yield_ctf: alloy::primitives::address!("38BF1cbD66d174bb5F3037d7068E708861D68D7f"),
            yield_neg_ctf: alloy::primitives::address!("26e865CbaAe99b62fbF9D18B55c25B5E079A93D5"),
            neg_adapter: alloy::primitives::address!("285c1B939380B130D7EBd09467b93faD4BA623Ed"),
            yield_neg_adapter: alloy::primitives::address!("b74aea04bdeBE912Aa425bC9173F9668e6f11F99"),
            yield_exchange: alloy::primitives::address!("8a6B4Fa700A1e310b106E7a48bAFa29111f66e89"),
            yield_neg_exchange: alloy::primitives::address!("95D5113bc50eD201e319101bbca3e0E250662fCC"),
            standard_exchange: alloy::primitives::address!("2A6413639BD3d73a20ed8C95F634Ce198ABbd2d7"),
            standard_neg_exchange: alloy::primitives::address!("d690b2bd441bE36431F6F6639D7Ad351e7B29680"),
        }),
        _ => None,
    }
}

pub fn exchange_for(is_yield_bearing: bool, is_neg_risk: bool, chain_id: u64) -> Address {
    let addrs = chain_addrs(chain_id).unwrap_or_else(|| chain_addrs(BNB_MAINNET).unwrap());
    match (is_yield_bearing, is_neg_risk) {
        (true, false) => addrs.yield_exchange,
        (true, true) => addrs.yield_neg_exchange,
        (false, false) => addrs.standard_exchange,
        (false, true) => addrs.standard_neg_exchange,
    }
}

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
}

#[derive(Clone, Copy, Debug)]
pub enum Side {
    Buy,
    /// Valid protocol side, unused by the v1 buy-only maker/taker lanes.
    #[allow(dead_code)]
    Sell,
}

pub struct OrderInput<'a> {
    pub token_id: &'a str,
    pub price: f64,
    pub quantity: f64,
    pub expiration: u64,
    pub fee_rate_bps: u64,
    pub side: Side,
    pub salt: u64,
    pub is_neg_risk: bool,
    pub is_yield_bearing: bool,
}

pub struct PredictSigner {
    key: PrivateKeySigner,
    account: Address,
    chain_id: u64,
}

impl PredictSigner {
    pub fn new(private_key: &str, account: &str, chain_id: u64) -> anyhow::Result<Self> {
        anyhow::ensure!(chain_addrs(chain_id).is_some(), "unsupported Predict chain {chain_id}");
        Ok(Self {
            key: PrivateKeySigner::from_str(private_key.trim())?,
            account: Address::from_str(account.trim())?,
            chain_id,
        })
    }

    pub fn account(&self) -> Address {
        self.account
    }

    fn exchange(&self, input: &OrderInput<'_>) -> Address {
        exchange_for(input.is_yield_bearing, input.is_neg_risk, self.chain_id)
    }

    fn order_domain(&self, exchange: Address) -> Eip712Domain {
        Eip712Domain::new(
            Some("predict.fun CTF Exchange".into()),
            Some("1".into()),
            Some(U256::from(self.chain_id)),
            Some(exchange),
            None,
        )
    }

    fn kernel_domain(&self) -> Eip712Domain {
        Eip712Domain::new(
            Some("Kernel".into()),
            Some("0.3.1".into()),
            Some(U256::from(self.chain_id)),
            Some(self.account),
            None,
        )
    }

    pub(crate) fn wrap_kernel_hash(&self, message_hash: B256) -> B256 {
        let type_hash = keccak256("Kernel(bytes32 hash)");
        let mut encoded = [0_u8; 64];
        encoded[..32].copy_from_slice(type_hash.as_slice());
        encoded[32..].copy_from_slice(message_hash.as_slice());
        let kernel_message = keccak256(encoded);
        let domain_separator = self.kernel_domain().hash_struct();
        let mut wrapped = [0_u8; 66];
        wrapped[..2].copy_from_slice(&[0x19, 0x01]);
        wrapped[2..34].copy_from_slice(domain_separator.as_slice());
        wrapped[34..].copy_from_slice(kernel_message.as_slice());
        keccak256(wrapped)
    }

    fn account_signature(&self, message_hash: B256) -> anyhow::Result<String> {
        let wrapped = self.wrap_kernel_hash(message_hash);
        // The SDK signs the 32 digest bytes with `signMessage`, so this is an
        // EIP-191 personal signature over the wrapped Kernel digest.
        let signature = self.key.sign_message_sync(wrapped.as_slice())?;
        let mut bytes = Vec::with_capacity(86);
        bytes.push(1);
        bytes.extend_from_slice(ECDSA_VALIDATOR.as_slice());
        bytes.extend_from_slice(&signature.as_bytes());
        Ok(format!("0x{}", alloy::hex::encode(bytes)))
    }

    pub fn sign_auth_message(&self, message: &str) -> anyhow::Result<String> {
        // ethers.hashMessage(message), used by Predict's official SDK.
        let prefix = format!("\x19Ethereum Signed Message:\n{}", message.len());
        let mut encoded = prefix.into_bytes();
        encoded.extend_from_slice(message.as_bytes());
        self.account_signature(keccak256(encoded))
    }

    pub fn sign_order(&self, input: &OrderInput<'_>) -> anyhow::Result<Value> {
        anyhow::ensure!(
            (0.0..=1.0).contains(&input.price) && input.quantity > 0.0,
            "invalid price or quantity"
        );
        const SCALE: u128 = 1_000_000_000_000_000_000;
        // Prices and sizes arrive as JSON numbers, but order amounts are exact
        // 18-decimal integers. Quantize the public API value first rather than
        // multiplying its binary floating-point representation by 1e18.
        let fixed = |value: f64| -> anyhow::Result<u128> {
            anyhow::ensure!(value.is_finite() && value >= 0.0, "non-finite order amount");
            let nanos = (value * 1_000_000_000.0).round() as u128;
            nanos.checked_mul(1_000_000_000).ok_or_else(|| anyhow::anyhow!("order amount overflow"))
        };
        let quantity = fixed(input.quantity)?;
        let price = fixed(input.price)?;
        anyhow::ensure!(quantity >= 10_000_000_000_000_000, "Predict quantity is below 0.01 shares");
        let usd =
            price.checked_mul(quantity).ok_or_else(|| anyhow::anyhow!("order notional overflow"))? / SCALE;
        let (maker_amount, taker_amount) = match input.side {
            Side::Buy => (usd, quantity),
            Side::Sell => (quantity, usd),
        };
        let order = Order {
            salt: U256::from(input.salt),
            maker: self.account,
            signer: self.account,
            taker: Address::ZERO,
            tokenId: U256::from_str(input.token_id)?,
            makerAmount: U256::from(maker_amount),
            takerAmount: U256::from(taker_amount),
            expiration: U256::from(input.expiration),
            nonce: U256::ZERO,
            feeRateBps: U256::from(input.fee_rate_bps),
            side: matches!(input.side, Side::Sell) as u8,
            signatureType: 0,
        };
        let hash = order.eip712_signing_hash(&self.order_domain(self.exchange(input)));
        let signature = self.account_signature(hash)?;
        Ok(json!({
            "salt": input.salt.to_string(), "maker": self.account.to_checksum(None),
            "signer": self.account.to_checksum(None), "taker": Address::ZERO.to_checksum(None),
            "tokenId": input.token_id, "makerAmount": maker_amount.to_string(),
            "takerAmount": taker_amount.to_string(), "expiration": input.expiration.to_string(),
            "nonce": "0", "feeRateBps": input.fee_rate_bps.to_string(),
            "side": matches!(input.side, Side::Sell) as u8, "signatureType": 0,
            "signature": signature, "hash": format!("{hash:#x}"),
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const KEY: &str = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318";

    /// The Privy signer for KEY; the smart account is a different address.
    const PRIVY: &str = "0x2c7536e3605d9c16a7a3d7b1898e529396a65c23";

    fn signer() -> PredictSigner {
        PredictSigner::new(KEY, "0x1111111111111111111111111111111111111111", BNB_MAINNET).unwrap()
    }

    fn order_input(side: Side) -> OrderInput<'static> {
        OrderInput {
            token_id: "1",
            price: 0.55,
            quantity: 2.0,
            expiration: 123,
            fee_rate_bps: 100,
            side,
            salt: 7,
            is_neg_risk: false,
            is_yield_bearing: true,
        }
    }

    #[test]
    fn signed_order_has_smart_account_envelope_and_exact_amounts() {
        let order = signer().sign_order(&order_input(Side::Buy)).unwrap();
        assert_eq!(order["makerAmount"], "1100000000000000000");
        assert_eq!(order["takerAmount"], "2000000000000000000");
        assert!(
            order["signature"].as_str().unwrap().starts_with("0x01845adb2c711129d4f3966735ed98a9f09fc4ce57")
        );
    }

    #[test]
    fn chain_tables_route_exchanges_and_reject_unknown_chains() {
        assert_eq!(
            exchange_for(true, false, BNB_MAINNET),
            alloy::primitives::address!("6bEb5a40C032AFc305961162d8204CDA16DECFa5")
        );
        assert_eq!(
            exchange_for(true, false, BNB_TESTNET),
            alloy::primitives::address!("8a6B4Fa700A1e310b106E7a48bAFa29111f66e89")
        );
        assert!(chain_addrs(1).is_none());
        assert!(PredictSigner::new(KEY, "0x1111111111111111111111111111111111111111", 1).is_err());
        // Same order on another chain signs a different hash (domain chain ID).
        let order = PredictSigner::new(KEY, "0x1111111111111111111111111111111111111111", BNB_TESTNET)
            .unwrap()
            .sign_order(&order_input(Side::Buy))
            .unwrap();
        assert_ne!(order["hash"], "0xcb75b80351dbe08c6e4ea6d0dc052f01ed414c6b1edf431a2a139cd0db6d155b");
    }

    #[test]
    fn sell_side_swaps_amounts_and_sets_side_bit() {
        let order = signer().sign_order(&order_input(Side::Sell)).unwrap();
        assert_eq!(order["makerAmount"], "2000000000000000000");
        assert_eq!(order["takerAmount"], "1100000000000000000");
        assert_eq!(order["side"], 1);
    }

    #[test]
    fn order_hash_and_signature_are_deterministic() {
        let signer = signer();
        let first = signer.sign_order(&order_input(Side::Buy)).unwrap();
        let second = signer.sign_order(&order_input(Side::Buy)).unwrap();
        assert_eq!(first["hash"], second["hash"]);
        assert_eq!(first["signature"], second["signature"]);
        // Golden values for the fixed input above (salt 7, expiry 123).
        assert_eq!(first["hash"], "0xcb75b80351dbe08c6e4ea6d0dc052f01ed414c6b1edf431a2a139cd0db6d155b");
        assert_eq!(
            first["signature"],
            "0x01845adb2c711129d4f3966735ed98a9f09fc4ce577bd856a7bf36aecde7c8c07750d56dd7d2532c243fc7453f5c4d769824997deb767fb6e115c9f716c3f51747128b8b86a14459d0a06126b639b6e16541fe702a1b"
        );
    }

    #[test]
    fn inner_signature_recovers_the_privy_signer() {
        use alloy::primitives::Signature;
        let signer = signer();
        // Order path: the EIP-712 order hash is Kernel-wrapped, then the
        // wrapped digest is personal-signed by the Privy key.
        let order = signer.sign_order(&order_input(Side::Buy)).unwrap();
        let hash = order["hash"].as_str().unwrap();
        let wrapped = signer.wrap_kernel_hash(hash.parse().unwrap());
        let envelope = order["signature"].as_str().unwrap();
        assert_eq!(&envelope[..44], "0x01845adb2c711129d4f3966735ed98a9f09fc4ce57");
        let inner = Signature::from_str(&format!("0x{}", &envelope[44..])).unwrap();
        assert_eq!(
            inner.recover_address_from_msg(wrapped.as_slice()).unwrap().to_checksum(None).to_lowercase(),
            PRIVY
        );
        // Auth path: ethers hashMessage, then the same Kernel wrap and envelope.
        let auth = signer.sign_auth_message("predict-auth-challenge").unwrap();
        assert_eq!(&auth[..44], "0x01845adb2c711129d4f3966735ed98a9f09fc4ce57");
        let prefix = format!("\x19Ethereum Signed Message:\n{}", "predict-auth-challenge".len());
        let mut encoded = prefix.into_bytes();
        encoded.extend_from_slice("predict-auth-challenge".as_bytes());
        let wrapped = signer.wrap_kernel_hash(keccak256(encoded));
        let inner = Signature::from_str(&format!("0x{}", &auth[44..])).unwrap();
        assert_eq!(
            inner.recover_address_from_msg(wrapped.as_slice()).unwrap().to_checksum(None).to_lowercase(),
            PRIVY
        );
    }
}
