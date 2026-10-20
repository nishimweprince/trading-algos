//! Kalshi request signing over `timestamp_ms + METHOD + path` (no query),
//! base64: RSA-PSS (SHA-256, salt = digest length) for an RSA key, or Ed25519
//! for an Ed25519 key (PKCS#8 PEM). The key's type picks the scheme.

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use ed25519_dalek::Signer as _;
use rsa::RsaPrivateKey;
use rsa::pkcs1::DecodeRsaPrivateKey;
use rsa::pkcs8::DecodePrivateKey;
use rsa::pss::BlindedSigningKey;
use rsa::signature::{RandomizedSigner, SignatureEncoding};
use sha2::Sha256;

#[derive(Clone)]
enum Key {
    Rsa(BlindedSigningKey<Sha256>),
    Ed25519(ed25519_dalek::SigningKey),
}

#[derive(Clone)]
pub struct KalshiSigner {
    pub key_id: String,
    key: Key,
}

impl std::fmt::Debug for KalshiSigner {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("KalshiSigner").field("key_id", &self.key_id).finish_non_exhaustive()
    }
}

impl KalshiSigner {
    pub fn from_pem(key_id: &str, pem: &str) -> anyhow::Result<Self> {
        let key = match RsaPrivateKey::from_pkcs8_pem(pem).or_else(|_| RsaPrivateKey::from_pkcs1_pem(pem)) {
            Ok(rsa) => Key::Rsa(BlindedSigningKey::<Sha256>::new(rsa)),
            Err(rsa_err) => Key::Ed25519(
                <ed25519_dalek::SigningKey as ed25519_dalek::pkcs8::DecodePrivateKey>::from_pkcs8_pem(pem)
                    .map_err(|e| {
                        anyhow::anyhow!(
                            "the Kalshi key must be an RSA or Ed25519 private key (PEM): rsa: {rsa_err}; ed25519: {e}"
                        )
                    })?,
            ),
        };
        Ok(Self { key_id: key_id.to_owned(), key })
    }

    pub fn sign(&self, message: &str) -> String {
        match &self.key {
            Key::Rsa(key) => STANDARD.encode(key.sign_with_rng(&mut rand::thread_rng(), message.as_bytes()).to_bytes()),
            Key::Ed25519(key) => STANDARD.encode(key.sign(message.as_bytes()).to_bytes()),
        }
    }

    pub fn headers(&self, method: &str, path: &str, ts_ms: Option<i64>) -> [(&'static str, String); 3] {
        let ts = ts_ms.unwrap_or_else(|| chrono::Utc::now().timestamp_millis()).to_string();
        let path = path.split('?').next().unwrap_or(path);
        [
            ("KALSHI-ACCESS-KEY", self.key_id.clone()),
            ("KALSHI-ACCESS-SIGNATURE", self.sign(&format!("{ts}{method}{path}"))),
            ("KALSHI-ACCESS-TIMESTAMP", ts),
        ]
    }
}

#[cfg(test)]
mod tests {
    use rsa::pkcs8::EncodePrivateKey;
    use rsa::pss::VerifyingKey;
    use rsa::signature::Verifier;

    use super::*;

    #[test]
    fn signs_verifiably() {
        let key = RsaPrivateKey::new(&mut rand::thread_rng(), 2048).unwrap();
        let pem = key.to_pkcs8_pem(rsa::pkcs8::LineEnding::LF).unwrap();
        let signer = KalshiSigner::from_pem("kid", &pem).unwrap();
        let h = signer.headers("GET", "/trade-api/ws/v2?x=1", Some(1_700_000_000_000));
        assert_eq!(h[2].1, "1700000000000");
        let sig = STANDARD.decode(&h[1].1).unwrap();
        let vk = VerifyingKey::<Sha256>::new(key.to_public_key());
        let sig = rsa::pss::Signature::try_from(sig.as_slice()).unwrap();
        vk.verify(b"1700000000000GET/trade-api/ws/v2", &sig).unwrap();
        assert!(KalshiSigner::from_pem("kid", "not a key").is_err());
    }

    #[test]
    fn signs_ed25519_verifiably() {
        use ed25519_dalek::pkcs8::EncodePrivateKey as _;
        let key = ed25519_dalek::SigningKey::from_bytes(&[7u8; 32]);
        let pem = key.to_pkcs8_pem(rsa::pkcs8::LineEnding::LF).unwrap();
        let signer = KalshiSigner::from_pem("kid", &pem).unwrap();
        let h = signer.headers("GET", "/trade-api/ws/v2?x=1", Some(1_700_000_000_000));
        let sig = STANDARD.decode(&h[1].1).unwrap();
        let sig = ed25519_dalek::Signature::from_slice(&sig).unwrap();
        key.verifying_key().verify_strict(b"1700000000000GET/trade-api/ws/v2", &sig).unwrap();
    }
}
