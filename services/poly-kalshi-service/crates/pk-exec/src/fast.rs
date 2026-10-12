//! The fast path's sender: a few warm HTTP/1.1 connections to the CLOB on which
//! the caller's thread writes a request that was built ahead of time.
//!
//! The usual order path (`reqwest` over HTTP/2) builds the request, signs the
//! L2 headers and hands it to a connection task: tens to hundreds of µs and a
//! thread hop. Here the request bytes are complete before the decision
//! ([`post_order_request`]), so sending is: pick an idle connection, encrypt,
//! one `write` on a `TCP_NODELAY` socket — on the caller's thread, with no task
//! switch. HTTP/1.1 carries one request at a time per connection, so a
//! connection is busy until its reply has been read.
//!
//! Each connection has a reader thread: it reads and decrypts the reply, parses
//! it (`Content-Length` or chunked), hands it to `on_reply` with the request's
//! tag, and frees the connection. Idle connections are kept warm with a
//! `GET /time` every [`KEEPALIVE`]; a closed or failed one is reconnected by
//! its reader, and is skipped by [`FastSender::send`] meanwhile.

use std::io::{ErrorKind, Read, Write};
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::{Duration, Instant};

use parking_lot::Mutex;
use rustls::pki_types::ServerName;
use serde::Serialize;

use crate::auth::L2Creds;

pub const KEEPALIVE: Duration = Duration::from_secs(15);
const READ_TIMEOUT: Duration = Duration::from_secs(1);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(3);
const REPLY_TIMEOUT: Duration = Duration::from_secs(10);

/// A reply to one tagged request.
#[derive(Clone, Debug)]
pub struct FastReply {
    pub tag: u64,
    pub t_written: Instant,
    pub t_reply: Instant,
    pub status: u16,
    pub body: Vec<u8>,
    pub error: Option<String>,
}

pub type OnReply = Arc<dyn Fn(FastReply) + Send + Sync>;

#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum SendError {
    #[error("no idle connection")]
    NoConnection,
    #[error("write: {0}")]
    Write(String),
}

/// `POST /order` with the L2 headers for `timestamp`, ready to write as is.
pub fn post_order_request(host: &str, creds: &L2Creds, body: &[u8], timestamp: u64) -> Vec<u8> {
    let mut req = Vec::with_capacity(body.len() + 512);
    let _ = write!(req, "POST /order HTTP/1.1\r\nhost: {host}\r\ncontent-type: application/json\r\n");
    for (k, v) in creds.headers(timestamp, "POST", "/order", body) {
        let _ = write!(req, "{k}: {v}\r\n");
    }
    let _ = write!(req, "content-length: {}\r\nconnection: keep-alive\r\n\r\n", body.len());
    req.extend_from_slice(body);
    req
}

fn ping_request(host: &str) -> Vec<u8> {
    format!("GET /time HTTP/1.1\r\nhost: {host}\r\nconnection: keep-alive\r\n\r\n").into_bytes()
}

/// A complete HTTP/1.1 response at the start of `buf`: its status, body and
/// length in `buf`; `None` while more bytes are needed.
pub fn parse_response(buf: &[u8]) -> Result<Option<(u16, Vec<u8>, usize)>, String> {
    let mut headers = [httparse::EMPTY_HEADER; 48];
    let mut resp = httparse::Response::new(&mut headers);
    let head = match resp.parse(buf).map_err(|e| format!("http: {e}"))? {
        httparse::Status::Complete(n) => n,
        httparse::Status::Partial => return Ok(None),
    };
    let status = resp.code.unwrap_or(0);
    let header = |name: &str| {
        let h = resp.headers.iter().find(|h| h.name.eq_ignore_ascii_case(name))?;
        Some(String::from_utf8_lossy(h.value))
    };
    if header("transfer-encoding").is_some_and(|v| v.to_ascii_lowercase().contains("chunked")) {
        let mut body = Vec::new();
        let mut at = head;
        loop {
            let Some(eol) = buf[at..].windows(2).position(|w| w == b"\r\n") else { return Ok(None) };
            let line = std::str::from_utf8(&buf[at..at + eol]).map_err(|e| format!("chunk: {e}"))?;
            let size = usize::from_str_radix(line.split(';').next().unwrap_or("").trim(), 16)
                .map_err(|e| format!("chunk size: {e}"))?;
            at += eol + 2;
            if size == 0 {
                // Trailers (none expected), then the blank line.
                let Some(end) = buf[at..].windows(2).position(|w| w == b"\r\n") else { return Ok(None) };
                return Ok(Some((status, body, at + end + 2)));
            }
            if buf.len() < at + size + 2 {
                return Ok(None);
            }
            body.extend_from_slice(&buf[at..at + size]);
            at += size + 2;
        }
    }
    let len = header("content-length").and_then(|v| v.trim().parse::<usize>().ok()).unwrap_or(0);
    if buf.len() < head + len {
        return Ok(None);
    }
    Ok(Some((status, buf[head..head + len].to_vec(), head + len)))
}

struct Live {
    tls: rustls::ClientConnection,
    sock: TcpStream,
}

#[derive(Clone, Copy)]
struct InFlight {
    tag: u64,
    t_written: Instant,
    ping: bool,
}

struct Conn {
    live: Mutex<Option<Live>>,
    busy: AtomicBool,
    up: AtomicBool,
    in_flight: Mutex<Option<InFlight>>,
    last_used: Mutex<Instant>,
}

#[derive(Debug, Default, Serialize)]
pub struct FastSenderStats {
    pub connections: usize,
    pub up: usize,
    pub busy: usize,
    pub sent: u64,
    pub replies: u64,
    pub no_connection: u64,
    pub reconnects: u64,
    pub errors: u64,
    pub last_error: Option<String>,
}

pub struct FastSender {
    host: String,
    port: u16,
    config: Arc<rustls::ClientConfig>,
    conns: Vec<Arc<Conn>>,
    on_reply: OnReply,
    sent: AtomicU64,
    replies: AtomicU64,
    no_connection: AtomicU64,
    reconnects: AtomicU64,
    errors: AtomicU64,
    last_error: Mutex<Option<String>>,
    stopping: AtomicBool,
}

impl FastSender {
    /// Open `n` connections to `base_url`'s host (each with its reader thread).
    /// Connections that fail now are retried by their readers.
    pub fn start(base_url: &str, n: usize, on_reply: OnReply) -> anyhow::Result<Arc<Self>> {
        let url = url::Url::parse(base_url)?;
        let host = url.host_str().ok_or_else(|| anyhow::anyhow!("no host in {base_url}"))?.to_owned();
        let port = url.port_or_known_default().unwrap_or(443);
        let roots = rustls::RootCertStore::from_iter(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
        let mut config = rustls::ClientConfig::builder().with_root_certificates(roots).with_no_client_auth();
        config.alpn_protocols = vec![b"http/1.1".to_vec()];
        let conns = (0..n.max(1))
            .map(|_| {
                Arc::new(Conn {
                    live: Mutex::new(None),
                    busy: AtomicBool::new(false),
                    up: AtomicBool::new(false),
                    in_flight: Mutex::new(None),
                    last_used: Mutex::new(Instant::now()),
                })
            })
            .collect();
        let sender = Arc::new(Self {
            host,
            port,
            config: Arc::new(config),
            conns,
            on_reply,
            sent: AtomicU64::new(0),
            replies: AtomicU64::new(0),
            no_connection: AtomicU64::new(0),
            reconnects: AtomicU64::new(0),
            errors: AtomicU64::new(0),
            last_error: Mutex::new(None),
            stopping: AtomicBool::new(false),
        });
        for (i, c) in sender.conns.iter().enumerate() {
            let (s, c) = (sender.clone(), c.clone());
            std::thread::Builder::new().name(format!("pk-fast-rd{i}")).spawn(move || s.reader(&c))?;
        }
        Ok(sender)
    }

    pub fn host(&self) -> &str {
        &self.host
    }

    /// Write `request` on an idle connection, tagged for its reply. Returns the
    /// instant the bytes were handed to the kernel. No allocation on this path
    /// beyond what TLS does internally.
    pub fn send(&self, tag: u64, request: &[u8]) -> Result<Instant, SendError> {
        for c in &self.conns {
            if !c.up.load(Ordering::Acquire) || c.busy.swap(true, Ordering::AcqRel) {
                continue;
            }
            match Self::write(c, request) {
                Ok(t) => {
                    *c.in_flight.lock() = Some(InFlight { tag, t_written: t, ping: false });
                    *c.last_used.lock() = t;
                    self.sent.fetch_add(1, Ordering::Relaxed);
                    return Ok(t);
                }
                Err(e) => {
                    c.up.store(false, Ordering::Release);
                    c.busy.store(false, Ordering::Release);
                    self.error(format!("write: {e}"));
                    return Err(SendError::Write(e.to_string()));
                }
            }
        }
        self.no_connection.fetch_add(1, Ordering::Relaxed);
        Err(SendError::NoConnection)
    }

    fn write(c: &Conn, request: &[u8]) -> std::io::Result<Instant> {
        let mut guard = c.live.lock();
        let live = guard.as_mut().ok_or_else(|| std::io::Error::new(ErrorKind::NotConnected, "down"))?;
        live.tls.writer().write_all(request)?;
        while live.tls.wants_write() {
            live.tls.write_tls(&mut live.sock)?;
        }
        Ok(Instant::now())
    }

    fn error(&self, e: String) {
        self.errors.fetch_add(1, Ordering::Relaxed);
        tracing::warn!(error = %e, "fast_sender");
        *self.last_error.lock() = Some(e);
    }

    /// Connect and complete the TLS handshake; returns the reader's half.
    fn connect(&self, c: &Conn) -> anyhow::Result<TcpStream> {
        let addr: SocketAddr = (self.host.as_str(), self.port)
            .to_socket_addrs()?
            .next()
            .ok_or_else(|| anyhow::anyhow!("{} did not resolve", self.host))?;
        let mut sock = TcpStream::connect_timeout(&addr, CONNECT_TIMEOUT)?;
        sock.set_nodelay(true)?;
        let name = ServerName::try_from(self.host.clone())?;
        let mut tls = rustls::ClientConnection::new(self.config.clone(), name)?;
        sock.set_read_timeout(Some(CONNECT_TIMEOUT))?;
        while tls.is_handshaking() {
            tls.complete_io(&mut sock)?;
        }
        let read = sock.try_clone()?;
        read.set_read_timeout(Some(READ_TIMEOUT))?;
        *c.live.lock() = Some(Live { tls, sock });
        *c.in_flight.lock() = None;
        c.busy.store(false, Ordering::Release);
        c.up.store(true, Ordering::Release);
        Ok(read)
    }

    /// Keep one connection: connect, read replies, ping when idle, reconnect.
    fn reader(&self, c: &Conn) {
        let mut buf = vec![0u8; 32 * 1024];
        let mut plain: Vec<u8> = Vec::with_capacity(32 * 1024);
        let mut backoff = Duration::from_millis(200);
        let mut read_sock: Option<TcpStream> = None;
        while !self.stopping.load(Ordering::Relaxed) {
            if !c.up.load(Ordering::Acquire) || read_sock.is_none() {
                c.up.store(false, Ordering::Release);
                self.fail_in_flight(c, "connection lost");
                match self.connect(c) {
                    Ok(read) => {
                        read_sock = Some(read);
                        self.reconnects.fetch_add(1, Ordering::Relaxed);
                        plain.clear();
                        backoff = Duration::from_millis(200);
                    }
                    Err(e) => {
                        self.error(format!("connect: {e:#}"));
                        std::thread::sleep(backoff);
                        backoff = (backoff * 2).min(Duration::from_secs(10));
                        continue;
                    }
                }
            }
            let Some(sock) = read_sock.as_mut() else { continue };
            match sock.read(&mut buf) {
                Ok(0) => {
                    c.up.store(false, Ordering::Release);
                }
                Ok(n) => {
                    if let Err(e) = Self::decrypt(c, &buf[..n], &mut plain) {
                        self.error(format!("tls: {e}"));
                        c.up.store(false, Ordering::Release);
                        continue;
                    }
                    self.drain_replies(c, &mut plain);
                }
                Err(e) if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) => {
                    self.idle(c);
                }
                Err(e) => {
                    self.error(format!("read: {e}"));
                    c.up.store(false, Ordering::Release);
                }
            }
        }
    }

    fn decrypt(c: &Conn, bytes: &[u8], plain: &mut Vec<u8>) -> anyhow::Result<()> {
        let mut guard = c.live.lock();
        let live = guard.as_mut().ok_or_else(|| anyhow::anyhow!("down"))?;
        let mut rd = bytes;
        while !rd.is_empty() {
            live.tls.read_tls(&mut rd)?;
            live.tls.process_new_packets()?;
        }
        let mut chunk = [0u8; 8192];
        loop {
            match live.tls.reader().read(&mut chunk) {
                Ok(0) => anyhow::bail!("closed by the server"),
                Ok(m) => plain.extend_from_slice(&chunk[..m]),
                Err(e) if e.kind() == ErrorKind::WouldBlock => break,
                Err(e) => return Err(e.into()),
            }
        }
        while live.tls.wants_write() {
            live.tls.write_tls(&mut live.sock)?;
        }
        Ok(())
    }

    fn drain_replies(&self, c: &Conn, plain: &mut Vec<u8>) {
        loop {
            match parse_response(plain) {
                Ok(Some((status, body, used))) => {
                    plain.drain(..used);
                    let flight = c.in_flight.lock().take();
                    c.busy.store(false, Ordering::Release);
                    if let Some(f) = flight.filter(|f| !f.ping) {
                        self.replies.fetch_add(1, Ordering::Relaxed);
                        (self.on_reply)(FastReply {
                            tag: f.tag,
                            t_written: f.t_written,
                            t_reply: Instant::now(),
                            status,
                            body,
                            error: None,
                        });
                    }
                }
                Ok(None) => return,
                Err(e) => {
                    self.error(e);
                    plain.clear();
                    c.up.store(false, Ordering::Release);
                    return;
                }
            }
        }
    }

    /// Nothing to read: ping an idle connection, give up on a reply that is late.
    fn idle(&self, c: &Conn) {
        let flight = *c.in_flight.lock();
        if let Some(f) = flight {
            if f.t_written.elapsed() > REPLY_TIMEOUT {
                self.fail_in_flight(c, "no reply");
                c.up.store(false, Ordering::Release);
            }
            return;
        }
        if c.last_used.lock().elapsed() < KEEPALIVE || c.busy.swap(true, Ordering::AcqRel) {
            return;
        }
        match Self::write(c, &ping_request(&self.host)) {
            Ok(t) => {
                *c.in_flight.lock() = Some(InFlight { tag: 0, t_written: t, ping: true });
                *c.last_used.lock() = t;
            }
            Err(e) => {
                self.error(format!("ping: {e}"));
                c.busy.store(false, Ordering::Release);
                c.up.store(false, Ordering::Release);
            }
        }
    }

    /// An order whose connection failed before its reply: report it, so the
    /// caller can find out what happened to it (the order may have landed).
    fn fail_in_flight(&self, c: &Conn, why: &str) {
        let flight = c.in_flight.lock().take();
        c.busy.store(false, Ordering::Release);
        if let Some(f) = flight.filter(|f| !f.ping) {
            (self.on_reply)(FastReply {
                tag: f.tag,
                t_written: f.t_written,
                t_reply: Instant::now(),
                status: 0,
                body: Vec::new(),
                error: Some(why.to_owned()),
            });
        }
    }

    pub fn stop(&self) {
        self.stopping.store(true, Ordering::Relaxed);
    }

    pub fn stats(&self) -> FastSenderStats {
        FastSenderStats {
            connections: self.conns.len(),
            up: self.conns.iter().filter(|c| c.up.load(Ordering::Relaxed)).count(),
            busy: self.conns.iter().filter(|c| c.busy.load(Ordering::Relaxed)).count(),
            sent: self.sent.load(Ordering::Relaxed),
            replies: self.replies.load(Ordering::Relaxed),
            no_connection: self.no_connection.load(Ordering::Relaxed),
            reconnects: self.reconnects.load(Ordering::Relaxed),
            errors: self.errors.load(Ordering::Relaxed),
            last_error: self.last_error.lock().clone(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_length_and_chunked_replies() {
        let one = b"HTTP/1.1 200 OK\r\ncontent-length: 5\r\n\r\nhello";
        assert_eq!(parse_response(one).unwrap(), Some((200, b"hello".to_vec(), one.len())));
        assert_eq!(parse_response(&one[..20]).unwrap(), None);
        assert_eq!(parse_response(&one[..one.len() - 1]).unwrap(), None);
        let chunked: &[u8] =
            b"HTTP/1.1 400 Bad\r\ntransfer-encoding: chunked\r\n\r\n4\r\nab\r\n\r\n3\r\ncde\r\n0\r\n\r\nNEXT";
        let (status, body, used) = parse_response(chunked).unwrap().unwrap();
        assert_eq!((status, body.as_slice()), (400, b"ab\r\ncde".as_slice()));
        assert_eq!(&chunked[used..], b"NEXT");
        assert_eq!(parse_response(&chunked[..chunked.len() - 7]).unwrap(), None);
    }

    #[test]
    fn builds_a_signed_post() {
        let creds = L2Creds::new(
            "0xabc".into(),
            "key".into(),
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            "pass".into(),
        )
        .unwrap();
        let body = br#"{"order":1}"#;
        let req = String::from_utf8(post_order_request("clob.example", &creds, body, 1_700_000_000)).unwrap();
        assert!(req.starts_with("POST /order HTTP/1.1\r\nhost: clob.example\r\n"));
        let sig = creds.signature(1_700_000_000, "POST", "/order", body);
        assert!(req.contains(&format!("POLY_SIGNATURE: {sig}\r\n")));
        assert!(req.contains("POLY_TIMESTAMP: 1700000000\r\n"));
        assert!(req.ends_with("content-length: 11\r\nconnection: keep-alive\r\n\r\n{\"order\":1}"));
    }
}
