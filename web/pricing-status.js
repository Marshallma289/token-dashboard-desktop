(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.CodexPricingStatus = api;
})(typeof globalThis === 'object' ? globalThis : this, function () {
  'use strict';

  function value(summary, ...keys) {
    for (const key of keys) {
      const raw = summary[key];
      if (raw != null && raw !== '' && Number.isFinite(Number(raw))) return Number(raw);
    }
    return null;
  }

  // Request counts catch unknown zero-token requests, which token coverage alone
  // cannot distinguish from fully priced usage. Priced zero is still a valid cost.
  function status(summary = {}) {
    const declared = summary.pricing_status ?? summary.pricingStatus;
    if (declared === 'unpriced' || declared === 'partial') return declared;
    const total = value(summary, 'total_tokens', 'totalTokens', 'total', 'tokens') ?? 0;
    const requests = value(summary, 'request_count', 'requestCount', 'requests') ?? 0;
    const pricedTokens = value(summary, 'priced_tokens', 'pricedTokens');
    const unpricedTokens = value(summary, 'unpriced_tokens', 'unpricedTokens');
    const pricedRequests = value(summary, 'priced_request_count', 'pricedRequestCount');
    const unpricedRequests = value(summary, 'unpriced_request_count', 'unpricedRequestCount');
    if (unpricedTokens > 0 || unpricedRequests > 0) {
      const knownPricedTokens = pricedTokens ?? (unpricedTokens == null ? 0 : total - unpricedTokens);
      const knownPricedRequests = pricedRequests ?? (unpricedRequests == null ? 0 : requests - unpricedRequests);
      return knownPricedTokens > 0 || knownPricedRequests > 0 ? 'partial' : 'unpriced';
    }
    if (declared === 'priced' || declared === 'free') return 'priced';
    if (pricedTokens > 0 || pricedRequests > 0) return 'priced';
    if (total > 0 || requests > 0) {
      const coverage = value(summary, 'pricing_coverage', 'pricingCoverage');
      if (coverage != null && coverage < 1) return coverage <= 0 ? 'unpriced' : 'partial';
      if (value(summary, 'cost_before_discount_usd', 'costBeforeDiscountUsd', 'costBefore',
        'cost_after_discount_usd', 'costAfterDiscountUsd', 'costAfter', 'estimated_cost_usd',
        'estimated_cost', 'cost_usd') === null) return 'unpriced';
    }
    return 'priced';
  }

  function label(priceStatus, amountText) {
    if (priceStatus === 'unpriced') return '未计价';
    if (priceStatus === 'partial') return `${amountText}（部分计价）`;
    return amountText;
  }

  function combine(statuses) {
    const values = new Set(statuses);
    return values.has('partial') || (values.has('unpriced') && values.has('priced'))
      ? 'partial' : values.has('unpriced') ? 'unpriced' : 'priced';
  }

  return { status, label, combine };
});
