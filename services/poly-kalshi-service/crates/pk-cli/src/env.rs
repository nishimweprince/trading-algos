//! Settings come from `.env.<profile>` next to the service, then the process
//! environment (which wins), as the Python services did.

use std::path::{Path, PathBuf};

pub fn load_profile(profile: Option<&str>, dir: &Path) -> anyhow::Result<Option<PathBuf>> {
    let Some(profile) = profile else { return Ok(None) };
    let path = dir.join(format!(".env.{profile}"));
    if !path.exists() {
        anyhow::bail!("{} not found", path.display());
    }
    // dotenvy never overrides variables already set in the environment.
    dotenvy::from_path(&path)?;
    Ok(Some(path))
}

pub fn var(key: &str) -> Option<String> {
    std::env::var(key).ok().map(|v| v.trim().to_owned()).filter(|v| !v.is_empty())
}

pub fn var_or(key: &str, default: &str) -> String {
    var(key).unwrap_or_else(|| default.to_owned())
}

pub fn require(key: &str) -> anyhow::Result<String> {
    let v = var(key).ok_or_else(|| anyhow::anyhow!("{key} is not set"))?;
    if v.starts_with("replace-with") {
        anyhow::bail!("{key} still holds its placeholder");
    }
    Ok(v)
}
