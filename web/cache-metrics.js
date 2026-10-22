(() => {
  'use strict';

  function finiteNumber(value) {
    if (value === null || value === undefined || value === '') return null;
    const number = typeof value === 'number' ? value : Number(String(value).replace(/[ ,]/g, ''));
    return Number.isFinite(number) ? number : null;
  }

  function calculateRate(options = {}) {
    const cached = finiteNumber(options.cachedTokens);
    if (cached === null) return null;
    const valid = value => value !== null && value >= 0 && value <= 1 ? value : null;
    if (options.hitRateProvided) return valid(finiteNumber(options.hitRate));
    if (options.denominatorProvided) {
      const denominator = finiteNumber(options.cacheReadInputTokens);
      return denominator !== null && denominator > 0 ? valid(cached / denominator) : null;
    }
    const availableCount = finiteNumber(options.availableCount);
    const requestCount = finiteNumber(options.requestCount);
    if (options.availabilityKnown && availableCount === 0) return null;
    if (availableCount !== null && requestCount !== null && requestCount > 0 && availableCount < requestCount) return null;
    if (options.available === false) return null;
    const denominator = finiteNumber(options.inputTokens);
    return denominator !== null && denominator > 0 ? valid(cached / denominator) : null;
  }

  function formatPercent(value, available = true) {
    const number = finiteNumber(value);
    return available && number !== null ? `${(number * 100).toFixed(1)}%` : '—';
  }

  const api = Object.freeze({ calculateRate, formatPercent });
  if (typeof globalThis !== 'undefined') globalThis.CodexCacheMetrics = api;
  if (typeof module === 'object' && module && module.exports) module.exports = api;
})();
