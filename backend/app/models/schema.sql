-- SQL migration script to initialize the Supabase / PostgreSQL database schema for the BD Automation System.

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Enable pgvector extension (required for semantic deduplication)
CREATE EXTENSION IF NOT EXISTS "vector";

-- Create Enums
CREATE TYPE userrole AS ENUM (
    'admin',
    'bd_user'
);

-- Table: users
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    supabase_user_id VARCHAR(255) NOT NULL UNIQUE,
    email VARCHAR(255) NOT NULL UNIQUE,
    full_name VARCHAR(255),
    role userrole NOT NULL DEFAULT 'bd_user',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    hashed_password VARCHAR(255)
);

-- Table: candidates
CREATE TABLE candidates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    phone VARCHAR(50),
    location VARCHAR(255) DEFAULT 'US',
    work_auth VARCHAR(50) DEFAULT 'us_authorized',
    tech_stack TEXT[] NOT NULL DEFAULT '{}',
    years_exp INTEGER,
    linkedin_url VARCHAR(500),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    google_refresh_token TEXT,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL
);

-- Table: companies
CREATE TABLE companies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL UNIQUE,
    domain VARCHAR(255),
    ats_type VARCHAR(50),
    rate_limit_config JSON,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Table: jobs
CREATE TABLE jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title VARCHAR(500) NOT NULL,
    company VARCHAR(255) NOT NULL,
    location VARCHAR(255),
    source VARCHAR(100) NOT NULL,
    source_url VARCHAR(2000) NOT NULL UNIQUE,
    canonical_url VARCHAR(2000),
    description TEXT,
    skills TEXT[] DEFAULT '{}',
    salary_min INTEGER,
    salary_max INTEGER,
    pay_period VARCHAR(20),
    job_type VARCHAR(20),
    posted_at TIMESTAMPTZ,
    embedding VECTOR(1536),
    is_duplicate BOOLEAN DEFAULT false,
    duplicate_of UUID REFERENCES jobs(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for Layer 2 deduplication
CREATE INDEX idx_jobs_company_title ON jobs (company, title);

-- Table: resumes
CREATE TABLE resumes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id UUID NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    file_url VARCHAR(1000) NOT NULL,
    parsed_json JSONB,
    is_base BOOLEAN DEFAULT false,
    tailored_for_job_id UUID REFERENCES jobs(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (candidate_id, version)
);

-- Table: applications
CREATE TABLE applications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id UUID NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    resume_id UUID REFERENCES resumes(id) ON DELETE SET NULL,
    cover_letter_url VARCHAR(1000),
    status VARCHAR(100) NOT NULL DEFAULT 'FOUND',
    fit_score NUMERIC(5,2),
    ats_score NUMERIC(5,2),
    combined_score NUMERIC(5,2),
    screenshot_url VARCHAR(1000),
    submitted_at TIMESTAMPTZ,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (candidate_id, job_id)
);

-- Table: application_history
CREATE TABLE application_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id UUID NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    from_status VARCHAR(100),
    to_status VARCHAR(100) NOT NULL,
    meta_data JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Table: emails
CREATE TABLE emails (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id UUID NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    application_id UUID REFERENCES applications(id) ON DELETE SET NULL,
    gmail_id VARCHAR(255) UNIQUE NOT NULL,
    from_addr VARCHAR(255),
    subject TEXT,
    body_text TEXT,
    classification VARCHAR(100),
    confidence NUMERIC(3,2),
    raw_json JSONB,
    received_at TIMESTAMPTZ,
    processed_at TIMESTAMPTZ
);

-- Table: interviews
CREATE TABLE interviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id UUID NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    round INTEGER,
    type VARCHAR(50),
    scheduled_at TIMESTAMPTZ,
    meeting_url VARCHAR(1000),
    interviewer_name VARCHAR(255),
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Table: cover_letters
CREATE TABLE cover_letters (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id UUID NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    candidate_id UUID NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    resume_id UUID REFERENCES resumes(id) ON DELETE SET NULL,
    file_url VARCHAR(1000) NOT NULL,
    source VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
