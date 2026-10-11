//! The process-wide rustls crypto provider.
//!
//! Both `ring` (tokio-tungstenite) and `aws-lc-rs` (alloy) are in the tree, so
//! rustls cannot pick a default by itself and every TLS handshake panics.
//! Every binary calls [`install_default_provider`] first in `main`.

/// Install `ring` as the process-wide rustls provider. Panics if another
/// provider was installed first: starting with ambiguous TLS is worse.
pub fn install_default_provider() {
    rustls::crypto::ring::default_provider()
        .install_default()
        .expect("install the ring CryptoProvider exactly once");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_provider_installs() {
        install_default_provider();
        assert!(rustls::crypto::CryptoProvider::get_default().is_some());
    }
}
