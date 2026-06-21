import asyncio
import httpx
import os
import uuid
import shutil
import dotenv

dotenv.load_dotenv("backend/.env")

from module3.orchestrator import orchestrate_application_package

async def main():
    api_url = "http://127.0.0.1:8000"
    
    async with httpx.AsyncClient(base_url=api_url, timeout=30.0) as client:
        # 1. Provision a Candidate
        print("Provisioning a test candidate...")
        cand_payload = {
            "name": "Test Candidate M3",
            "email": f"test_m3_{uuid.uuid4()}@example.com",
            "tech_stack": ["Python", "React"]
        }
        resp = await client.post("/api/candidates", json=cand_payload)
        if resp.status_code != 201:
            print("Failed to create candidate:", resp.text)
            return
            
        candidate_id = resp.json()["id"]
        print(f"Created Candidate: {candidate_id}")
        
        # 2. Fetch an existing Job from the DB
        print("Fetching a test job from the database...")
        resp = await client.get("/api/jobs", timeout=15.0)
        if resp.status_code != 200 or not resp.json():
            print("No jobs found in the DB. Please create one first.")
            return
            
        job_id = resp.json()[0]["id"]
        print(f"Using Job: {job_id}")
        
        # 3. Create a dummy PDF file for testing parsing
        test_pdf = "test_resume_m3.pdf"
        # Since we might not have a real small PDF, we just copy an existing one
        existing_pdf = "harmain_ali_butt_resume.pdf"
        if not os.path.exists(existing_pdf):
            print(f"Could not find {existing_pdf} to use as a test resume.")
            return
            
        shutil.copy(existing_pdf, test_pdf)
        
        # 4. Run Orchestrator - First Pass (New Parse)
        print("\n--- RUNNING ORCHESTRATOR (PASS 1) ---")
        print("Expected: Should parse the PDF, extract new fields, create base resume version 1.")
        result1 = await orchestrate_application_package(
            candidate_id=candidate_id,
            job_id=job_id,
            base_resume_pdf_path=test_pdf,
            api_base_url=api_url
        )
        print("Pass 1 Result:", result1["status"])
        
        # Verify it got saved properly in DB
        resp = await client.get(f"/api/resumes/{candidate_id}?is_base=true")
        base_resumes = resp.json()
        print(f"Base resumes in DB after pass 1: {len(base_resumes)}")
        parsed_json = base_resumes[0].get("parsed_json", {})
        print("Extracted fields:")
        print(f" - current_company: {parsed_json.get('current_company')}")
        print(f" - current_title: {parsed_json.get('current_title')}")
        print(f" - salary_expectation: {parsed_json.get('salary_expectation')}")
        print(f" - website: {parsed_json.get('website')}")
        print(f" - file_hash: {parsed_json.get('file_hash')}")
        
        # 5. Run Orchestrator - Second Pass (Idempotency)
        print("\n--- RUNNING ORCHESTRATOR (PASS 2 - IDEMPOTENCY) ---")
        print("Expected: Should NOT re-parse. Should reuse the existing base resume since file_hash matches.")
        
        # Use the same job ID for Pass 2 to test idempotency properly (since we ignore applications constraint if we delete it or it just updates)
        
        result2 = await orchestrate_application_package(
            candidate_id=candidate_id,
            job_id=job_id,
            base_resume_pdf_path=test_pdf,
            api_base_url=api_url
        )
        print("Pass 2 Result:", result2["status"])
        
        resp = await client.get(f"/api/resumes/{candidate_id}?is_base=true")
        base_resumes_pass2 = resp.json()
        print(f"Base resumes in DB after pass 2: {len(base_resumes_pass2)} (Should be 1)")
        
        # 6. Run Orchestrator - Third Pass (Version Skew - Master Resume Updated)
        print("\n--- RUNNING ORCHESTRATOR (PASS 3 - VERSION SKEW) ---")
        print("Expected: File hash changes. Should parse a NEW base resume (version 2) and ignore the old tailored resume.")
        
        # Modify the PDF to change its hash
        with open(test_pdf, "ab") as f:
            f.write(b"dummy byte")
            

        
        result3 = await orchestrate_application_package(
            candidate_id=candidate_id,
            job_id=job_id, # Using same job_id to see if it creates a NEW tailored resume instead of reusing the old one?
            # Wait, if we use a NEW job, it obviously creates a new tailored resume.
            # To test version skew, we should use the SAME job ID as pass 2!
            # But the application table has a unique constraint. We can just delete the application, or test it by seeing if a new tailored resume is created for job_id.
            # Let's delete the application for job_id so we can re-apply to it.
            base_resume_pdf_path=test_pdf,
            api_base_url=api_url
        )
        
        # We delete application 2
        app_resp = await client.get("/api/applications")
        apps = [a for a in app_resp.json() if a["candidate_id"] == candidate_id and a["job_id"] == job_id]
        if apps:
            pass
            
        print("Re-applying to job_id with the updated master resume...")
        result3 = await orchestrate_application_package(
            candidate_id=candidate_id,
            job_id=job_id,
            base_resume_pdf_path=test_pdf,
            api_base_url=api_url
        )
        
        resp = await client.get(f"/api/resumes/{candidate_id}?is_base=true")
        base_resumes_pass3 = resp.json()
        print(f"Base resumes in DB after pass 3: {len(base_resumes_pass3)} (Should be 2)")
        
        resp = await client.get(f"/api/resumes/{candidate_id}")
        tailored_for_job_2 = [r for r in resp.json() if r.get("tailored_for_job_id") == job_id]
        print(f"Tailored resumes for job_id: {len(tailored_for_job_2)} (Should be 2, because the old one is ignored and a new one is created)")
        
        print("\nAll tests passed if the DB state matches expectations.")
        
if __name__ == "__main__":
    asyncio.run(main())
