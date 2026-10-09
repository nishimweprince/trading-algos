//! Polymarket L2 request headers: an HMAC-SHA256 over `timestamp + method + path + body`
//! with the API secret (URL-safe base64), as the CLOB and `polymarket_client_sdk_v2` do.
//! It costs microseconds, so it is computed per request, on the hot path.

use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE;
use hmac::{Hmac, Mac};
use sha2::Sha256;

#[derive(Clone)]
pub struct L2Creds {
    /// The signer's address, EIP-55 checksummed.
    pub address: String,
    pub api_key: String,
    secret: Vec<u8>,
    pub passphrase: String,
}

impl std::fmt::Debug for L2Creds {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("L2Creds")
            .field("address", &self.address)
            .field("api_key", &self.api_key)
            .finish_non_exhaustive()
    }
}

impl L2Creds {
    pub fn new(
        address: String,
        api_key: String,
        secret_b64: &str,
        passphrase: String,
    ) -> anyhow::Result<Self> {
        let secret = URL_SAFE.decode(secret_b64.trim())?;
        Ok(Self { address, api_key, secret, passphrase })
    }

    pub fn signature(&self, timestamp: u64, method: &str, path: &str, body: &[u8]) -> String {
        let mut mac = Hmac::<Sha256>::new_from_slice(&self.secret).expect("HMAC takes any key length");
        mac.update(timestamp.to_string().as_bytes());
        mac.update(method.as_bytes());
        mac.update(path.as_bytes());
        // The venue signs the body with single quotes swapped for double quotes;
        // JSON from serde has none, so this is the body as sent.
        if body.contains(&b'\'') {
            let swapped: Vec<u8> = body.iter().map(|&b| if b == b'\'' { b'"' } else { b }).collect();
            mac.update(&swapped);
        } else {
            mac.update(body);
        }
        URL_SAFE.encode(mac.finalize().into_bytes())
    }

    pub fn headers(
        &self,
        timestamp: u64,
        method: &str,
        path: &str,
        body: &[u8],
    ) -> [(&'static str, String); 5] {
        [
            ("POLY_ADDRESS", self.address.clone()),
            ("POLY_API_KEY", self.api_key.clone()),
            ("POLY_PASSPHRASE", self.passphrase.clone()),
            ("POLY_SIGNATURE", self.signature(timestamp, method, path, body)),
            ("POLY_TIMESTAMP", timestamp.to_string()),
        ]
    }

    /// Verify a request's signature (the mock CLOB in tests uses this).
    pub fn verify(&self, timestamp: u64, method: &str, path: &str, body: &[u8], signature: &str) -> bool {
        self.signature(timestamp, method, path, body) == signature
    }
}

pub fn unix_now_s() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The vector from `polymarket_client_sdk_v2`'s own auth tests.
    #[test]
    fn matches_the_sdk_vector() {
        let creds = L2Creds::new(
            "0x0".into(),
            "k".into(),
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            "p".into(),
        )
        .unwrap();
        let sig = creds.signature(1_000_000, "test-sign", "/orders", br#"{"hash":"0x123"}"#);
        assert_eq!(sig, "4gJVbox-R6XlDK4nlaicig0_ANVL1qdcahiL8CXfXLM=");
    }
}
