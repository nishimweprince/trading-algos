//! Golden vectors for the C++ maker (`services/pk-maker-cpp`): order bodies,
//! signatures and auth headers from `polymarket_client_sdk_v2`'s own types and
//! alloy's EIP-712, for fixed keys, salts and timestamps.
//!
//! `PKM_VECTORS_OUT=../pk-maker-cpp/tests/fixtures/clob_vectors.txt cargo test -p pk-exec --test clob_vectors -- --ignored`

use std::fmt::Write as _;
use std::str::FromStr;

use alloy::primitives::{Address, B256, U256, address, keccak256};
use alloy::signers::SignerSync as _;
use alloy::signers::local::PrivateKeySigner;
use alloy::sol_types::{Eip712Domain, SolStruct, SolValue, eip712_domain};
use pk_exec::auth::L2Creds;
use polymarket_client_sdk_v2::auth::ApiKey;
use polymarket_client_sdk_v2::clob::types::{OrderPayload, OrderSignature, OrderType, OrderV2, SignedOrder};

const KEY: &str = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80";
const FUNDER: Address = address!("0x59316c3d15C292D2c7a9EC206eCB6c6bAAdeAd3e");
const TOKEN: &str = "71321045679252212594626385532706912750332728571942532289631379312455583992563";
const API_KEY: &str = "0b0c7c6e-2b8f-4f4e-9d55-7c2f3b1a9e11";
const SECRET: &str = "c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0LXNlY3I=";
const CONDITION: &str = "0xaabc35349bc01a07a0ad8d863866175165a0640fddd0985e69f94570e0054c42";

// Copied from the SDK's private `sign_poly1271_order` (clob/client.rs).
const ORDER_TYPE_STRING: &str = concat!(
    "Order(uint256 salt,address maker,address signer,uint256 tokenId,",
    "uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,",
    "uint256 timestamp,bytes32 metadata,bytes32 builder)"
);
const SOLADY_TYPE_STRING: &str = concat!(
    "TypedDataSign(Order contents,string name,string version,uint256 chainId,",
    "address verifyingContract,bytes32 salt)",
    "Order(uint256 salt,address maker,address signer,uint256 tokenId,",
    "uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,",
    "uint256 timestamp,bytes32 metadata,bytes32 builder)"
);

mod typed {
    alloy::sol! {
        struct ClobAuth { address address; string timestamp; uint256 nonce; string message; }
        struct Call { address target; uint256 value; bytes data; }
        struct Batch { address wallet; uint256 nonce; uint256 deadline; Call[] calls; }
        function redeemPositions(address collateralToken, bytes32 parentCollectionId, bytes32 conditionId, uint256[] indexSets);
    }
}

fn order(signer: Address, maker: Address, sig_type: u8, salt: u64, maker_amount: u64, taker_amount: u64) -> OrderV2 {
    let mut o = OrderV2::default();
    o.salt = U256::from(salt);
    o.maker = maker;
    o.signer = signer;
    o.tokenId = U256::from_str(TOKEN).unwrap();
    o.makerAmount = U256::from(maker_amount);
    o.takerAmount = U256::from(taker_amount);
    o.side = 0;
    o.signatureType = sig_type;
    o.timestamp = U256::from(1_761_500_000_123_u64);
    o
}

fn exchange_domain() -> Eip712Domain {
    eip712_domain! {
        name: "Polymarket CTF Exchange",
        version: "2",
        chain_id: 137,
        verifying_contract: address!("0xE111180000d2663C0091e4f400237545B87B996B"),
    }
}

fn wrapped(signer: &PrivateKeySigner, o: &OrderV2) -> String {
    let domain = exchange_domain();
    let contents_hash = o.eip712_hash_struct();
    let app = domain.hash_struct();
    let typed = keccak256(
        (
            keccak256(SOLADY_TYPE_STRING.as_bytes()),
            contents_hash,
            keccak256("DepositWallet".as_bytes()),
            keccak256("1".as_bytes()),
            U256::from(137),
            o.signer,
            B256::ZERO,
        )
            .abi_encode(),
    );
    let mut input = [0_u8; 66];
    input[0] = 0x19;
    input[1] = 0x01;
    input[2..34].copy_from_slice(app.as_slice());
    input[34..66].copy_from_slice(typed.as_slice());
    let sig = signer.sign_hash_sync(&keccak256(input)).unwrap();
    let mut s = sig.to_string();
    s.push_str(&alloy::hex::encode(app));
    s.push_str(&alloy::hex::encode(contents_hash));
    s.push_str(&alloy::hex::encode(ORDER_TYPE_STRING.as_bytes()));
    s.push_str(&alloy::hex::encode((ORDER_TYPE_STRING.len() as u16).to_be_bytes()));
    s
}

fn body(o: OrderV2, sig: OrderSignature) -> String {
    let signed = SignedOrder::builder()
        .payload(OrderPayload::new(o, U256::ZERO))
        .signature(sig)
        .order_type(OrderType::GTC)
        .owner(ApiKey::parse_str(API_KEY).unwrap())
        .post_only(true)
        .build();
    serde_json::to_string(&signed).unwrap()
}

#[test]
#[ignore = "writes the C++ maker's golden vectors"]
fn write_vectors() {
    let signer = PrivateKeySigner::from_str(KEY).unwrap();
    let eoa = signer.address();
    let mut out = String::new();
    writeln!(out, "key={KEY}").unwrap();
    writeln!(out, "address={}", eoa.to_checksum(None)).unwrap();
    writeln!(out, "funder={}", FUNDER.to_checksum(None)).unwrap();
    writeln!(out, "token={TOKEN}").unwrap();
    writeln!(out, "api_key={API_KEY}").unwrap();
    writeln!(out, "secret={SECRET}").unwrap();
    writeln!(out, "domain_v2=0x{}", alloy::hex::encode(exchange_domain().hash_struct())).unwrap();

    // EOA: 5 shares at 0.47.
    let o = order(eoa, eoa, 0, 123_456_789, 2_350_000, 5_000_000);
    writeln!(out, "eoa_struct_hash=0x{}", alloy::hex::encode(o.eip712_hash_struct())).unwrap();
    let sig = signer.sign_hash_sync(&o.eip712_signing_hash(&exchange_domain())).unwrap();
    writeln!(out, "eoa_signature={sig}").unwrap();
    let eoa_body = body(o, OrderSignature::Ecdsa(sig));
    writeln!(out, "eoa_body={eoa_body}").unwrap();

    // Deposit wallet: maker and signer are the wallet, 7 shares at 0.31.
    let o = order(FUNDER, FUNDER, 3, 987_654_321, 2_170_000, 7_000_000);
    writeln!(out, "dw_struct_hash=0x{}", alloy::hex::encode(o.eip712_hash_struct())).unwrap();
    let sig = wrapped(&signer, &o);
    writeln!(out, "dw_signature={sig}").unwrap();
    let dw_body = body(o, OrderSignature::Wrapped(sig));
    writeln!(out, "dw_body={dw_body}").unwrap();

    // L2 headers over the deposit-wallet body.
    let creds = L2Creds::new(eoa.to_checksum(None), API_KEY.into(), SECRET, "pp".into()).unwrap();
    writeln!(out, "l2_post_order={}", creds.signature(1_761_500_000, "POST", "/order", dw_body.as_bytes())).unwrap();
    let cancel = br#"{"orderID":"0xabc"}"#;
    writeln!(out, "l2_cancel={}", creds.signature(1_761_500_001, "DELETE", "/order", cancel)).unwrap();
    writeln!(out, "l2_get={}", creds.signature(1_761_500_002, "GET", "/data/orders", b"")).unwrap();

    // L1 ClobAuth.
    let auth = typed::ClobAuth {
        address: eoa,
        timestamp: "1761500000".into(),
        nonce: U256::ZERO,
        message: "This message attests that I control the given wallet".into(),
    };
    let auth_domain = eip712_domain! { name: "ClobAuthDomain", version: "1", chain_id: 137, };
    let h = auth.eip712_signing_hash(&auth_domain);
    writeln!(out, "clob_auth_digest=0x{}", alloy::hex::encode(h)).unwrap();
    writeln!(out, "clob_auth_signature={}", signer.sign_hash_sync(&h).unwrap()).unwrap();

    // Relayer batch: one redeemPositions call on the adapter.
    let data = alloy::sol_types::SolCall::abi_encode(&typed::redeemPositionsCall {
        collateralToken: address!("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"),
        parentCollectionId: B256::ZERO,
        conditionId: B256::from_str(CONDITION).unwrap(),
        indexSets: vec![U256::from(1), U256::from(2)],
    });
    writeln!(out, "condition={CONDITION}").unwrap();
    writeln!(out, "redeem_calldata=0x{}", alloy::hex::encode(&data)).unwrap();
    let batch = typed::Batch {
        wallet: FUNDER,
        nonce: U256::from(2),
        deadline: U256::from(1_761_500_300_u64),
        calls: vec![typed::Call {
            target: address!("0xAdA100Db00Ca00073811820692005400218FcE1f"),
            value: U256::ZERO,
            data: data.into(),
        }],
    };
    let batch_domain = eip712_domain! { name: "DepositWallet", version: "1", chain_id: 137, verifying_contract: FUNDER, };
    let h = batch.eip712_signing_hash(&batch_domain);
    writeln!(out, "batch_digest=0x{}", alloy::hex::encode(h)).unwrap();
    writeln!(out, "batch_signature=0x{}", alloy::hex::encode(signer.sign_hash_sync(&h).unwrap().as_bytes())).unwrap();

    match std::env::var("PKM_VECTORS_OUT") {
        Ok(path) => std::fs::write(path, out).unwrap(),
        Err(_) => print!("{out}"),
    }
}
