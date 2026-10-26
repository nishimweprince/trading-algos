//! Hyperliquid L1 action signing. The ordered value tree is shared by the
//! MessagePack hash and JSON payload so the signed bytes cannot diverge from
//! the submitted action.

use std::str::FromStr;

use alloy::primitives::{Address, B256, U256, keccak256};
use alloy::signers::SignerSync as _;
use alloy::signers::local::PrivateKeySigner;
use alloy::sol_types::{Eip712Domain, SolStruct};

#[derive(Clone, Debug, PartialEq)]
pub enum Node {
    /// Reserved for nullable action fields; no v1 action encodes one.
    #[allow(dead_code)]
    Null,
    Bool(bool),
    Uint(u64),
    Str(String),
    Array(Vec<Node>),
    Map(Vec<(String, Node)>),
}

impl Node {
    pub fn msgpack(&self, out: &mut Vec<u8>) {
        match self {
            Self::Null => out.push(0xc0),
            Self::Bool(v) => out.push(if *v { 0xc3 } else { 0xc2 }),
            Self::Uint(v) => pack_uint(*v, out),
            Self::Str(v) => pack_str(v, out),
            Self::Array(values) => {
                pack_header(values.len(), 0x90, 0xdc, 0xdd, out);
                for v in values {
                    v.msgpack(out);
                }
            }
            Self::Map(fields) => {
                pack_header(fields.len(), 0x80, 0xde, 0xdf, out);
                for (k, v) in fields {
                    pack_str(k, out);
                    v.msgpack(out);
                }
            }
        }
    }
    pub fn json(&self) -> serde_json::Value {
        match self {
            Self::Null => serde_json::Value::Null,
            Self::Bool(v) => (*v).into(),
            Self::Uint(v) => (*v).into(),
            Self::Str(v) => v.clone().into(),
            Self::Array(v) => serde_json::Value::Array(v.iter().map(Node::json).collect()),
            Self::Map(v) => serde_json::Value::Object(v.iter().map(|(k, v)| (k.clone(), v.json())).collect()),
        }
    }
}

fn put_be(out: &mut Vec<u8>, value: u64, bytes: usize) {
    for i in (0..bytes).rev() {
        out.push((value >> (8 * i)) as u8)
    }
}
fn pack_uint(v: u64, out: &mut Vec<u8>) {
    if v < 0x80 {
        out.push(v as u8)
    } else if v <= 0xff {
        out.push(0xcc);
        put_be(out, v, 1)
    } else if v <= 0xffff {
        out.push(0xcd);
        put_be(out, v, 2)
    } else if v <= 0xffff_ffff {
        out.push(0xce);
        put_be(out, v, 4)
    } else {
        out.push(0xcf);
        put_be(out, v, 8)
    }
}
fn pack_str(v: &str, out: &mut Vec<u8>) {
    let n = v.len();
    if n < 32 {
        out.push(0xa0 | n as u8)
    } else if n <= 255 {
        out.push(0xd9);
        put_be(out, n as u64, 1)
    } else if n <= 65535 {
        out.push(0xda);
        put_be(out, n as u64, 2)
    } else {
        out.push(0xdb);
        put_be(out, n as u64, 4)
    }
    out.extend_from_slice(v.as_bytes())
}
fn pack_header(n: usize, fix: u8, h16: u8, h32: u8, out: &mut Vec<u8>) {
    if n < 16 {
        out.push(fix | n as u8)
    } else if n <= 65535 {
        out.push(h16);
        put_be(out, n as u64, 2)
    } else {
        out.push(h32);
        put_be(out, n as u64, 4)
    }
}

#[derive(Clone, Debug)]
pub struct OrderSpec {
    pub asset: u32,
    pub buy: bool,
    pub price: String,
    pub size: String,
    pub reduce_only: bool,
    pub tif: &'static str,
    pub cloid: Option<String>,
}

fn order_wire(o: &OrderSpec) -> Node {
    let mut f = vec![
        ("a".into(), Node::Uint(o.asset as u64)),
        ("b".into(), Node::Bool(o.buy)),
        ("p".into(), Node::Str(o.price.clone())),
        ("s".into(), Node::Str(o.size.clone())),
        ("r".into(), Node::Bool(o.reduce_only)),
        (
            "t".into(),
            Node::Map(vec![("limit".into(), Node::Map(vec![("tif".into(), Node::Str(o.tif.into()))]))]),
        ),
    ];
    if let Some(c) = &o.cloid {
        f.push(("c".into(), Node::Str(c.clone())))
    }
    Node::Map(f)
}

pub fn order_action(orders: &[OrderSpec]) -> Node {
    Node::Map(vec![
        ("type".into(), Node::Str("order".into())),
        ("orders".into(), Node::Array(orders.iter().map(order_wire).collect())),
        ("grouping".into(), Node::Str("na".into())),
    ])
}
pub fn cancel_by_cloid(asset: u32, cloid: &str) -> Node {
    cancel_cloids(&[(asset, cloid.to_owned())])
}
pub fn cancel_cloids(cancels: &[(u32, String)]) -> Node {
    Node::Map(vec![
        ("type".into(), Node::Str("cancelByCloid".into())),
        (
            "cancels".into(),
            Node::Array(
                cancels
                    .iter()
                    .map(|(asset, cloid)| {
                        Node::Map(vec![
                            ("asset".into(), Node::Uint(*asset as u64)),
                            ("cloid".into(), Node::Str(cloid.clone())),
                        ])
                    })
                    .collect(),
            ),
        ),
    ])
}
pub fn schedule_cancel(time: Option<u64>) -> Node {
    let mut f = vec![("type".into(), Node::Str("scheduleCancel".into()))];
    if let Some(t) = time {
        f.push(("time".into(), Node::Uint(t)))
    }
    Node::Map(f)
}

alloy::sol! { #[derive(Debug)] struct Agent { string source; bytes32 connectionId; } }

pub fn action_hash(action: &Node, nonce: u64, expires: u64) -> B256 {
    let mut bytes = Vec::new();
    action.msgpack(&mut bytes);
    put_be(&mut bytes, nonce, 8);
    bytes.push(0);
    if expires != 0 {
        bytes.push(0);
        put_be(&mut bytes, expires, 8)
    }
    keccak256(bytes)
}
pub fn digest(action: &Node, nonce: u64, expires: u64) -> B256 {
    let agent = Agent { source: "a".into(), connectionId: action_hash(action, nonce, expires) };
    let domain = Eip712Domain::new(
        Some("Exchange".into()),
        Some("1".into()),
        Some(U256::from(1337)),
        Some(Address::ZERO),
        None,
    );
    agent.eip712_signing_hash(&domain)
}

pub struct HlSigner {
    key: PrivateKeySigner,
}
impl HlSigner {
    pub fn new(key: &str) -> anyhow::Result<Self> {
        Ok(Self { key: PrivateKeySigner::from_str(key.trim())? })
    }
    pub fn address(&self) -> Address {
        self.key.address()
    }
    pub fn payload(&self, action: &Node, nonce: u64, expires: u64) -> anyhow::Result<serde_json::Value> {
        let sig = self.key.sign_hash_sync(&digest(action, nonce, expires))?;
        let bytes = sig.as_bytes();
        let r = trim_hex(&bytes[..32]);
        let s = trim_hex(&bytes[32..64]);
        let raw_v = bytes[64];
        let v = if raw_v < 27 { raw_v + 27 } else { raw_v };
        let mut value = serde_json::json!({"action":action.json(),"nonce":nonce,"signature":{"r":r,"s":s,"v":v},"vaultAddress":null});
        if expires != 0 {
            value["expiresAfter"] = expires.into()
        }
        Ok(value)
    }
}
fn trim_hex(bytes: &[u8]) -> String {
    let encoded = alloy::hex::encode(bytes);
    let trimmed = encoded.trim_start_matches('0');
    format!("0x{}", if trimmed.is_empty() { "0" } else { trimmed })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    /// hyperliquid-python-sdk 0.24.0 vectors, shared with the C++ reference
    /// (`services/hyperliquid-arb/tests/fixtures/hl_sign_vectors.txt`, whose
    /// `test_signing_vectors` passes). Format per row:
    /// name|expires_after|msgpack_hex|action_hash_hex|r|s|v|action_json.
    /// Nonce is 1761024000123 for every row.
    const VECTORS: &str = include_str!("../../../../hyperliquid-arb/tests/fixtures/hl_sign_vectors.txt");
    const KEY: &str = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318";
    const NONCE: u64 = 1761024000123;

    fn cloid(last: u8) -> String {
        format!("0x{:032x}", last as u128)
    }

    fn actions() -> Vec<(String, Node)> {
        let bid = || OrderSpec {
            asset: 110024,
            buy: true,
            price: "157.84".into(),
            size: "6.33".into(),
            reduce_only: false,
            tif: "Alo",
            cloid: Some(cloid(1)),
        };
        let ask = || OrderSpec {
            asset: 110024,
            buy: false,
            price: "157.86".into(),
            size: "6.33".into(),
            reduce_only: false,
            tif: "Alo",
            cloid: Some(cloid(2)),
        };
        vec![
            ("order_alo".into(), order_action(&[bid(), ask()])),
            ("order_alo_expires".into(), order_action(&[bid()])),
            (
                "order_ioc".into(),
                order_action(&[OrderSpec {
                    asset: 0,
                    buy: false,
                    price: "83000".into(),
                    size: "0.00123".into(),
                    reduce_only: true,
                    tif: "Ioc",
                    cloid: None,
                }]),
            ),
            ("cancel_by_cloid".into(), cancel_by_cloid(110024, &cloid(1))),
            ("schedule_cancel".into(), schedule_cancel(Some(1761024060000))),
            ("schedule_cancel_clear".into(), schedule_cancel(None)),
        ]
    }

    #[test]
    fn official_sdk_vectors_match_msgpack_hash_signature_and_json() {
        let built: std::collections::HashMap<String, Node> = actions().into_iter().collect();
        let signer = HlSigner::new(KEY).unwrap();
        let mut checked = 0;
        for line in VECTORS.lines() {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            let f: Vec<&str> = line.split('|').collect();
            if f[0] == "address" {
                assert_eq!(signer.address().to_checksum(None).to_lowercase(), f[1]);
                continue;
            }
            let Some(action) = built.get(f[0]) else { continue };
            let expires: u64 = f[1].parse().unwrap();
            let mut packed = Vec::new();
            action.msgpack(&mut packed);
            assert_eq!(alloy::hex::encode(&packed), f[2], "{0}: msgpack", f[0]);
            assert_eq!(format!("{:x}", action_hash(action, NONCE, expires)), f[3], "{0}: hash", f[0]);
            let p = signer.payload(action, NONCE, expires).unwrap();
            assert_eq!(p["signature"]["r"], f[4], "{0}: r", f[0]);
            assert_eq!(p["signature"]["s"], f[5], "{0}: s", f[0]);
            assert_eq!(p["signature"]["v"].as_u64().unwrap().to_string(), f[6], "{0}: v", f[0]);
            assert!(p.get("vaultAddress").is_some_and(Value::is_null), "{0}: vaultAddress", f[0]);
            if expires == 0 {
                assert!(p.get("expiresAfter").is_none(), "{0}: no expiresAfter", f[0]);
            } else {
                assert_eq!(p["expiresAfter"], expires, "{0}: expiresAfter", f[0]);
            }
            let expected: Value = serde_json::from_str(f[7]).unwrap();
            assert_eq!(action.json(), expected, "{0}: JSON", f[0]);
            assert_eq!(p["action"], expected, "{0}: payload action", f[0]);
            assert_eq!(p["nonce"], NONCE, "{0}: payload nonce", f[0]);
            checked += 1;
        }
        assert_eq!(checked, built.len(), "every ported action matched a vector row");
    }

    #[test]
    fn expiry_changes_the_signed_hash() {
        let action = order_action(&[OrderSpec {
            asset: 100175880,
            buy: true,
            price: "0.42".into(),
            size: "1".into(),
            reduce_only: false,
            tif: "Alo",
            cloid: Some(cloid(9)),
        }]);
        assert_ne!(action_hash(&action, NONCE, 0), action_hash(&action, NONCE, NONCE + 5000));
    }
}
