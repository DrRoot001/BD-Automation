// Minimal CSV parsing for the job-URL fallback file.
// Accepts: a plain list of URLs (one per line), or a CSV with headers where
// one column contains application URLs (optionally title/company columns).
// URLs may be Indeed listings OR direct company/ATS application pages.

export function parseCsv(text) {
  const rows = [];
  let row = [], cell = '', inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') { cell += '"'; i++; }
        else inQuotes = false;
      } else cell += c;
    } else if (c === '"') {
      inQuotes = true;
    } else if (c === ',') {
      row.push(cell); cell = '';
    } else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++;
      row.push(cell); cell = '';
      if (row.some(v => v.trim() !== '')) rows.push(row);
      row = [];
    } else {
      cell += c;
    }
  }
  row.push(cell);
  if (row.some(v => v.trim() !== '')) rows.push(row);
  return rows;
}

const isJobUrl = (v) => {
  try {
    const u = new URL(String(v).trim());
    return u.protocol === 'http:' || u.protocol === 'https:';
  } catch { return false; }
};

/**
 * Extract job entries from CSV text.
 * @returns [{url, title?, company?}]
 */
export function extractJobs(text) {
  const rows = parseCsv(text);
  if (!rows.length) return [];

  // Locate the URL column: header named url/link, else first column with a URL.
  const header = rows[0].map(h => h.trim().toLowerCase());
  const looksLikeHeader = !rows[0].some(isJobUrl);
  let urlCol = header.findIndex(h => /\b(url|link|job.?url|apply.?url)\b/.test(h));
  const dataRows = looksLikeHeader ? rows.slice(1) : rows;
  if (urlCol === -1) {
    const probe = dataRows.find(r => r.some(isJobUrl));
    urlCol = probe ? probe.findIndex(isJobUrl) : -1;
  }
  if (urlCol === -1) return [];

  const titleCol = looksLikeHeader ? header.findIndex(h => /title|position|role/.test(h)) : -1;
  const companyCol = looksLikeHeader ? header.findIndex(h => /company|employer/.test(h)) : -1;

  const seen = new Set();
  const jobs = [];
  for (const r of dataRows) {
    const url = (r[urlCol] || '').trim();
    if (!isJobUrl(url) || seen.has(url)) continue;
    seen.add(url);
    jobs.push({
      url,
      title: titleCol >= 0 ? (r[titleCol] || '').trim() || null : null,
      company: companyCol >= 0 ? (r[companyCol] || '').trim() || null : null,
    });
  }
  return jobs;
}
