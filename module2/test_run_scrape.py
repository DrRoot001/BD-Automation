from module2.run_scrape import map_item_to_job_create


def test_map_item_keeps_job_when_optional_fields_are_missing():
    item = {
        "title": "Software Engineer",
        "company": "Example Corp",
        "location": "Remote",
        "source_url": "https://example.com/jobs/1",
        "job_type": "Full Time",
        "pay_period": "yearly",
    }

    mapped = map_item_to_job_create(item, "https://example.com/jobs", "engineering")

    assert mapped is not None
    assert mapped["description"] == ""
    assert mapped["skills"] == []
    assert mapped["salary_min"] is None
    assert mapped["salary_max"] is None


def test_map_item_accepts_remote_us_locations():
    for location in ["Remote", "Remote in United States", "Remote in California", "Remote - CA", "Remote, US"]:
        item = {
            "title": "Software Engineer",
            "company": "Example Corp",
            "location": location,
            "source_url": "https://example.com/jobs/2",
            "job_type": "Full Time",
            "pay_period": "yearly",
        }

        mapped = map_item_to_job_create(item, "https://example.com/jobs", "engineering")
        assert mapped is not None, location
