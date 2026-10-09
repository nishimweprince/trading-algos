//! Logs: human-readable on stderr, and one JSON object per line in the events
//! log (`EVENTS_LOG_PATH`), which the export ships.

use std::path::Path;

use tracing_appender::non_blocking::WorkerGuard;
use tracing_subscriber::EnvFilter;
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;

/// Keep the guard alive for the process: dropping it flushes the file.
pub fn init(level: &str, events_log: Option<&Path>) -> Option<WorkerGuard> {
    let filter =
        || EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new(level.to_lowercase()));
    let stderr = tracing_subscriber::fmt::layer().with_writer(std::io::stderr).with_filter_fn(filter());
    let (file_layer, guard) = match events_log {
        Some(path) => {
            if let Some(dir) = path.parent() {
                let _ = std::fs::create_dir_all(dir);
            }
            match std::fs::File::options().create(true).append(true).open(path) {
                Ok(f) => {
                    let (w, g) = tracing_appender::non_blocking(f);
                    let layer = tracing_subscriber::fmt::layer().json().flatten_event(true).with_writer(w);
                    (Some(layer.with_filter_fn(filter())), Some(g))
                }
                Err(e) => {
                    eprintln!("events log {}: {e}", path.display());
                    (None, None)
                }
            }
        }
        None => (None, None),
    };
    let _ = tracing_subscriber::registry().with(stderr).with(file_layer).try_init();
    guard
}

trait WithFilter<S>: tracing_subscriber::Layer<S> + Sized
where
    S: tracing::Subscriber,
{
    fn with_filter_fn(self, f: EnvFilter) -> tracing_subscriber::filter::Filtered<Self, EnvFilter, S> {
        tracing_subscriber::Layer::with_filter(self, f)
    }
}

impl<S: tracing::Subscriber, L: tracing_subscriber::Layer<S>> WithFilter<S> for L {}
