async function loadManifest(defaults) {
  try {
    const res = await fetch('./manifest.json', { cache: 'no-store' });
    if (res.ok) return { ...defaults, ...(await res.json()) };
  } catch (_) {}
  return defaults;
}
function assetUrl(manifest, key, fallback = '') {
  const asset = (manifest.assets || []).find((item) => item.key === key);
  return asset?.previewUrl || asset?.url || fallback;
}
function text(value, fallback) { return value || fallback; }
window.StudioComposition = { loadManifest, assetUrl, text };
