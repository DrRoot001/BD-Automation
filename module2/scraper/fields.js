// module2/scraper/fields.js
// Fixed extraction template — encodes the standing scraping rules so every
// link uses the same AI extraction instructions. Edit this file when the
// rules change; no need to touch scrape.js.

export const FIELDS = {
  title:
    'The full job title as listed in the posting (e.g. "Senior Machine Learning Engineer").',
  company:
    'The name of the hiring company or organization.',
  location:
    'Work location. Must be one of: "Fully Remote", "Remote", "Remote in United States". ' +
    'If the job is not explicitly remote, leave this blank.',
  source_url:
    'The full direct URL of this specific job posting (the apply or detail page, not the search results page).',
  canonical_url:
    'If there is a canonical or permanent link for this job posting different from the listing URL, provide it. ' +
    'Otherwise leave blank.',
  description:
    'A concise summary of the job responsibilities and requirements in 2–4 sentences. ' +
    'Do not copy the full description — summarize it.',
  skills:
    'A comma-separated list of specific technical skills, tools, or technologies required or preferred ' +
    '(e.g. "Python, TensorFlow, AWS, SQL"). Only include concrete skills, not soft skills.',
  salary_min:
    'The minimum salary or pay rate as a plain integer. ' +
    'For annual salaries use the yearly amount (e.g. 120000). ' +
    'For hourly rates use the hourly amount (e.g. 50). ' +
    'Leave blank if not mentioned or below $120,000/yr or $50/hr.',
  salary_max:
    'The maximum salary or pay rate as a plain integer. ' +
    'For annual salaries use the yearly amount (e.g. 180000). ' +
    'For hourly rates use the hourly amount (e.g. 80). ' +
    'Leave blank if not mentioned.',
  pay_period:
    'The pay period. Must be exactly one of: "yearly", "hourly", "monthly". ' +
    'Leave blank if salary is not mentioned.',
  job_type:
    'Employment type. Must be exactly one of: Full Time, Contract, 1099, C2C, ' +
    'Full Time/Contract, Freelance, Part Time. ' +
    'Internship is NOT acceptable — if the posting is an internship, leave this blank.',
  posted_at:
    'The date this job was posted, in ISO 8601 format (YYYY-MM-DD). ' +
    'If only a relative time is shown (e.g. "3 days ago"), calculate from today. ' +
    'Leave blank if unknown.',
  clearance_required:
    'Answer "Yes" if the posting mentions any security clearance requirement or the ability to obtain one ' +
    '(e.g. Public Trust, Secret, Top Secret, TS/SCI). Otherwise answer "No".',
};

// Hard safety ceiling per link — overridable via SCRAPE_LIMIT env var.
export const DEFAULT_LIMIT = Number(process.env.SCRAPE_LIMIT || 200);
