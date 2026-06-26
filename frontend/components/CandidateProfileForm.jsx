import React, { useState } from 'react';

export default function CandidateProfileForm() {
  const [formData, setFormData] = useState({
    name: '',
    email: '',
    phone: '',
    location: 'US',
    work_auth: 'us_authorized',
    tech_stack: '',
    years_exp: '',
    linkedin_url: '',
    resume_file: null
  });

  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState('');

  const handleChange = (e) => {
    const { name, value } = e.target;
    setFormData((prev) => ({ ...prev, [name]: value }));
  };

  const handleFileChange = (e) => {
    setFormData((prev) => ({ ...prev, resume_file: e.target.files[0] }));
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setSubmitting(true);
    setMessage('');

    try {
      // 1. Submit candidate metadata
      const candidatePayload = {
        name: formData.name,
        email: formData.email,
        phone: formData.phone || null,
        location: formData.location,
        work_auth: formData.work_auth,
        tech_stack: formData.tech_stack.split(',').map(item => item.trim()).filter(Boolean),
        years_exp: formData.years_exp ? parseInt(formData.years_exp) : null,
        linkedin_url: formData.linkedin_url || null,
      };

      const res = await fetch('/api/candidates', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(candidatePayload)
      });

      if (!res.ok) throw new Error('Failed to create candidate profile.');

      const candidateData = await res.json();
      const candidateId = candidateData.id;

      // 2. Upload base resume if selected
      if (formData.resume_file && candidateId) {
        const fileData = new FormData();
        fileData.append('file', formData.resume_file);
        fileData.append('candidate_id', candidateId);
        fileData.append('is_base', 'true');

        const uploadRes = await fetch(`/api/candidates/${candidateId}/resumes`, {
          method: 'POST',
          body: fileData
        });

        if (!uploadRes.ok) throw new Error('Candidate profile created, but resume upload failed.');
      }

      setMessage('Candidate profile and base resume uploaded successfully!');
      setFormData({
        name: '',
        email: '',
        phone: '',
        location: 'US',
        work_auth: 'us_authorized',
        tech_stack: '',
        years_exp: '',
        linkedin_url: '',
        resume_file: null
      });
    } catch (err) {
      setMessage(`Error: ${err.message}`);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={styles.card}>
      <h2 style={styles.title}>Input Candidate Profile</h2>
      <p style={styles.subtitle}>Enter the developer profile and upload their base resume to trigger the automation agent.</p>
      
      {message && (
        <div style={message.startsWith('Error') ? styles.errorBanner : styles.successBanner}>
          {message}
        </div>
      )}

      <form onSubmit={handleSubmit} style={styles.form}>
        <div style={styles.row}>
          <div style={styles.group}>
            <label style={styles.label}>Full Name *</label>
            <input type="text" name="name" required value={formData.name} onChange={handleChange} style={styles.input} placeholder="John Doe" />
          </div>
          <div style={styles.group}>
            <label style={styles.label}>Email Address *</label>
            <input type="email" name="email" required value={formData.email} onChange={handleChange} style={styles.input} placeholder="johndoe@example.com" />
          </div>
        </div>

        <div style={styles.row}>
          <div style={styles.group}>
            <label style={styles.label}>Phone Number</label>
            <input type="tel" name="phone" value={formData.phone} onChange={handleChange} style={styles.input} placeholder="+1 (555) 019-2834" />
          </div>
          <div style={styles.group}>
            <label style={styles.label}>LinkedIn URL</label>
            <input type="url" name="linkedin_url" value={formData.linkedin_url} onChange={handleChange} style={styles.input} placeholder="https://linkedin.com/in/username" />
          </div>
        </div>

        <div style={styles.row}>
          <div style={styles.group}>
            <label style={styles.label}>Location / Country</label>
            <input type="text" name="location" value={formData.location} onChange={handleChange} style={styles.input} />
          </div>
          <div style={styles.group}>
            <label style={styles.label}>Work Authorization</label>
            <select name="work_auth" value={formData.work_auth} onChange={handleChange} style={styles.select}>
              <option value="us_authorized">US Authorized (Citizen/GC)</option>
              <option value="visa_required">Requires Visa Sponsor</option>
              <option value="remote_global">Remote Global</option>
            </select>
          </div>
        </div>

        <div style={styles.row}>
          <div style={styles.group}>
            <label style={styles.label}>Years of Experience</label>
            <input type="number" name="years_exp" value={formData.years_exp} onChange={handleChange} style={styles.input} placeholder="5" min="0" />
          </div>
          <div style={styles.group}>
            <label style={styles.label}>Tech Stack (comma separated) *</label>
            <input type="text" name="tech_stack" required value={formData.tech_stack} onChange={handleChange} style={styles.input} placeholder="React, Node.js, Python, AWS" />
          </div>
        </div>

        <div style={styles.group}>
          <label style={styles.label}>Upload Base Resume (PDF / Word) *</label>
          <input type="file" required accept=".pdf,.doc,.docx" onChange={handleFileChange} style={styles.fileInput} />
        </div>

        <button type="submit" disabled={submitting} style={styles.button}>
          {submitting ? 'Creating Profile...' : 'Save & Onboard Candidate'}
        </button>
      </form>
    </div>
  );
}

const styles = {
  card: {
    backgroundColor: '#ffffff',
    borderRadius: '12px',
    boxShadow: '0 4px 20px rgba(0, 0, 0, 0.05)',
    padding: '32px',
    maxWidth: '800px',
    margin: '20px auto',
    fontFamily: 'Inter, system-ui, sans-serif'
  },
  title: {
    fontSize: '24px',
    fontWeight: '700',
    color: '#1e293b',
    margin: '0 0 8px 0'
  },
  subtitle: {
    fontSize: '14px',
    color: '#64748b',
    margin: '0 0 24px 0'
  },
  form: {
    display: 'flex',
    flexDirection: 'column',
    gap: '20px'
  },
  row: {
    display: 'flex',
    gap: '20px',
    flexWrap: 'wrap'
  },
  group: {
    flex: '1 1 300px',
    display: 'flex',
    flexDirection: 'column',
    gap: '6px'
  },
  label: {
    fontSize: '14px',
    fontWeight: '600',
    color: '#334155'
  },
  input: {
    padding: '10px 14px',
    borderRadius: '8px',
    border: '1px solid #cbd5e1',
    fontSize: '14px',
    outline: 'none',
    color: '#0f172a'
  },
  select: {
    padding: '10px 14px',
    borderRadius: '8px',
    border: '1px solid #cbd5e1',
    fontSize: '14px',
    backgroundColor: '#ffffff',
    outline: 'none',
    color: '#0f172a'
  },
  fileInput: {
    padding: '8px 0',
    fontSize: '14px',
    color: '#475569'
  },
  button: {
    backgroundColor: '#2563eb',
    color: '#ffffff',
    padding: '12px 24px',
    border: 'none',
    borderRadius: '8px',
    fontSize: '14px',
    fontWeight: '600',
    cursor: 'pointer',
    transition: 'background-color 0.2s',
    alignSelf: 'flex-start'
  },
  successBanner: {
    backgroundColor: '#dcfce7',
    color: '#15803d',
    padding: '12px 16px',
    borderRadius: '8px',
    fontSize: '14px',
    fontWeight: '500',
    marginBottom: '20px'
  },
  errorBanner: {
    backgroundColor: '#fee2e2',
    color: '#b91c1c',
    padding: '12px 16px',
    borderRadius: '8px',
    fontSize: '14px',
    fontWeight: '500',
    marginBottom: '20px'
  }
};
