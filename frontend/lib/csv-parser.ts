export interface RawJobInput {
  title?: string
  company?: string
  location?: string | null
  source?: string
  source_url?: string
  canonical_url?: string | null
  description?: string | null
  skills?: string[] | string
  salary_min?: number | string | null
  salary_max?: number | string | null
  pay_period?: string | null
  job_type?: string | null
  posted_at?: string | null
  [key: string]: unknown
}

export interface ParsedJob {
  title: string
  company: string
  location: string | null
  source: string
  source_url: string
  canonical_url: string | null
  description: string | null
  skills: string[]
  salary_min: number | null
  salary_max: number | null
  pay_period: string | null
  job_type: string | null
  posted_at: string | null
  isValid: boolean
  errors: string[]
}

/**
 * Parse raw CSV string into array of object key-values
 */
export function parseCSV(text: string): Record<string, string>[] {
  const lines: string[][] = []
  let row: string[] = []
  let inQuotes = false
  let currentField = ''

  for (let i = 0; i < text.length; i++) {
    const char = text[i]
    const nextChar = text[i + 1]

    if (char === '"') {
      if (inQuotes && nextChar === '"') {
        currentField += '"'
        i++ // skip escaped quote
      } else {
        inQuotes = !inQuotes
      }
    } else if (char === ',' && !inQuotes) {
      row.push(currentField.trim())
      currentField = ''
    } else if ((char === '\n' || char === '\r') && !inQuotes) {
      if (char === '\r' && nextChar === '\n') {
        i++
      }
      row.push(currentField.trim())
      if (row.length > 1 || row[0] !== '') {
        lines.push(row)
      }
      row = []
      currentField = ''
    } else {
      currentField += char
    }
  }

  if (row.length > 0 || currentField !== '') {
    row.push(currentField.trim())
    lines.push(row)
  }

  if (lines.length < 2) return []

  const headers = lines[0].map((h) => h.toLowerCase().replace(/['"’“”]/g, '').trim())
  const results: Record<string, string>[] = []

  for (let i = 1; i < lines.length; i++) {
    const values = lines[i]
    if (values.length === 0 || (values.length === 1 && values[0] === '')) continue

    const item: Record<string, string> = {}
    headers.forEach((header, index) => {
      item[header] = values[index] || ''
    })
    results.push(item)
  }

  return results
}

/**
 * Map raw CSV key-value row to standardized job schema
 */
export function mapCSVRowToJob(row: Record<string, string>): RawJobInput {
  const getValue = (keys: string[]) => {
    for (const key of keys) {
      if (row[key] !== undefined && row[key] !== '') return row[key]
    }
    return undefined
  }

  const title = getValue(['title', 'job title', 'job_title', 'position', 'role']) || ''
  const company = getValue(['company', 'company name', 'company_name', 'organization']) || ''
  const location = getValue(['location', 'job location', 'job_location', 'city', 'country']) || null
  const source = getValue(['source', 'origin']) || 'csv_import'
  const source_url = getValue(['source_url', 'source url', 'url', 'link', 'job url', 'job_url']) || ''
  const canonical_url = getValue(['canonical_url', 'canonical url', 'canonical_url_link']) || null
  const description = getValue(['description', 'job description', 'job_description', 'summary', 'details', 'body']) || null

  const skillsVal = getValue(['skills', 'key skills', 'requirements', 'technologies', 'tags'])
  let skills: string[] = []
  if (skillsVal) {
    if (skillsVal.startsWith('[') && skillsVal.endsWith(']')) {
      try {
        skills = JSON.parse(skillsVal)
      } catch {
        skills = skillsVal.split(',').map((s) => s.trim()).filter(Boolean)
      }
    } else {
      skills = skillsVal.split(',').map((s) => s.trim()).filter(Boolean)
    }
  }

  const parseMin = getValue(['salary_min', 'salary min', 'min_salary', 'salary_minimum', 'min salary'])
  const salary_min = parseMin ? parseInt(parseMin.replace(/[^0-9]/g, ''), 10) || null : null

  const parseMax = getValue(['salary_max', 'salary max', 'max_salary', 'salary_maximum', 'max salary'])
  const salary_max = parseMax ? parseInt(parseMax.replace(/[^0-9]/g, ''), 10) || null : null

  const pay_period = getValue(['pay_period', 'pay period', 'payment_period', 'frequency']) || null
  const job_type = getValue(['job_type', 'job type', 'type']) || null

  const posted_at_str = getValue(['posted_at', 'posted at', 'date', 'posted'])
  let posted_at = null
  if (posted_at_str) {
    try {
      const date = new Date(posted_at_str)
      if (!isNaN(date.getTime())) {
        posted_at = date.toISOString()
      }
    } catch {
      // ignore
    }
  }

  return {
    title,
    company,
    location,
    source,
    source_url,
    canonical_url,
    description,
    skills,
    salary_min,
    salary_max,
    pay_period,
    job_type,
    posted_at,
  }
}

/**
 * Validate and normalize array of job objects
 */
export function validateJobInputs(jobsList: RawJobInput[]): ParsedJob[] {
  return jobsList.map((job) => {
    const errors: string[] = []

    const title = (job.title || '').trim()
    const company = (job.company || '').trim()
    const source_url = (job.source_url || '').trim()

    let source = (job.source || '').trim()
    if (!source && source_url) {
      try {
        const url = new URL(source_url)
        source = url.hostname.replace('www.', '')
      } catch {
        source = 'imported'
      }
    }

    if (!title) errors.push('Title is required')
    if (!company) errors.push('Company is required')
    if (!source) errors.push('Source is required')
    if (!source_url) errors.push('Source URL is required')

    let skills: string[] = []
    if (Array.isArray(job.skills)) {
      skills = job.skills
    } else if (typeof job.skills === 'string') {
      skills = job.skills.split(',').map((s: string) => s.trim()).filter(Boolean)
    }

    let salary_min: number | null = null
    if (job.salary_min !== undefined && job.salary_min !== null && job.salary_min !== '') {
      salary_min =
        typeof job.salary_min === 'number'
          ? job.salary_min
          : parseInt(String(job.salary_min).replace(/[^0-9]/g, ''), 10) || null
    }

    let salary_max: number | null = null
    if (job.salary_max !== undefined && job.salary_max !== null && job.salary_max !== '') {
      salary_max =
        typeof job.salary_max === 'number'
          ? job.salary_max
          : parseInt(String(job.salary_max).replace(/[^0-9]/g, ''), 10) || null
    }

    return {
      title,
      company,
      location: job.location || null,
      source,
      source_url,
      canonical_url: job.canonical_url || null,
      description: job.description || null,
      skills,
      salary_min,
      salary_max,
      pay_period: job.pay_period || null,
      job_type: job.job_type || null,
      posted_at: job.posted_at || null,
      isValid: errors.length === 0,
      errors,
    }
  })
}
