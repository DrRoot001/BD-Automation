# module2/links.py
# Defines job-portal search URLs organised by day of week.
#
# Two schedules:
#   MON_LINKS  – heavier / additional sources run on Mondays only
#   OTHER_LINKS – standard sources run every other day (Tue–Sun)
#
# Call get_links() at runtime — it reads the system clock and returns
# the correct list automatically.

from __future__ import annotations

import datetime
from typing import Literal

# ──────────────────────────────────────────────────────────────────────────────
# Monday schedule
# ──────────────────────────────────────────────────────────────────────────────
MON_LINKS: dict[str, list[str]] = {
    "ML": [
        "https://www.indeed.com/jobs?q=Machine+Learning&l=Remote&fromage=3&sc=0kf%3Aattr%28DSQF7%29%3B&from=searchOnDesktopSerp&vjk=37d67eb7d5499d38",
        "https://www.dice.com/jobs?filters.postedDate=THREE&filters.workplaceTypes=Remote&q=Machine+Learning&location=United+States&radiusUnit=mi&latitude=38.7945952&longitude=-106.5348379&countryCode=US&locationPrecision=Country",
        "https://www.ziprecruiter.com/jobs-search?search=Machine+Learning&location=Remote+%28USA%29&radius=25&days=5&refine_by_employment=&refine_by_location_type=&refine_by_salary=&refine_by_salary_ceil=&refine_by_apply_type=&refine_by_experience_level=junior%2Cmid%2Csenior&seniority_filters_explicitly_set=true&apply_types_explicitly_set=true&employment_types_explicitly_set=true&location_types_explicitly_set=true&lk=tRXcTGOMLLbw4M_3Bv7Pzw",
        "https://www.glassdoor.com/Job/united-states-machine-learning-engineer-jobs-SRCH_IL.0,13_IN1_KO14,39.htm?remoteWorkType=1&fromAge=3",
        "https://jooble.org/SearchResult?date=2&loc=2&rgns=United%20States&ukw=machine%20learning",
        "https://www.talent.com/jobs?k=Machine+Learning&l=United+States&workplace=remote&radius=100&date=3",
        "https://www.adzuna.com/search?f=3&loc=151946&remote_only=1&q=Machine%20Learning",
    ],
    "Data": [
        "https://www.indeed.com/jobs?q=Data+Engineer&l=Remote&fromage=3&sc=0kf%3Aattr%28DSQF7%29%3B&from=searchOnDesktopSerp&vjk=bf7369a247f59fd5",
        "https://www.dice.com/jobs?countryCode=US&filters.postedDate=THREE&filters.workplaceTypes=Remote&latitude=38.7945952&location=United+States&locationPrecision=Country&longitude=-106.5348379&q=data+engineer&radiusUnit=mi",
        "https://www.ziprecruiter.com/jobs-search?search=Data+Engineer&location=United+States&radius=25&days=5&refine_by_employment=&refine_by_location_type=only_remote&refine_by_salary=&refine_by_salary_ceil=&refine_by_apply_type=&refine_by_experience_level=junior%2Cmid%2Csenior&seniority_filters_explicitly_set=true&location_types_explicitly_set=true&lk=tRXcTGOMLLbw4M_3Bv7Pzw",
        "https://www.glassdoor.com/Job/united-states-data-engineer-jobs-SRCH_IL.0,13_IN1_KO14,27.htm?remoteWorkType=1&fromAge=3",
        "https://jooble.org/SearchResult?date=2&loc=2&rgns=United%20States&ukw=data%20engineering%20",
        "https://www.talent.com/jobs?k=Data+Engineering&l=United+States&workplace=remote&radius=100&date=3",
    ],
    "ServiceNow": [
        "https://www.indeed.com/jobs?q=ServiceNow&l=Remote&fromage=3&sc=0kf%3Aattr%28DSQF7%29%3B&from=searchOnDesktopSerp&vjk=489d26f44d26f106",
        "https://www.dice.com/jobs?filters.postedDate=THREE&filters.workplaceTypes=Remote&countryCode=US&latitude=38.7945952&location=United+States&locationPrecision=Country&longitude=-106.5348379&q=ServiceNow&radiusUnit=mi",
        "https://www.ziprecruiter.com/jobs-search?search=ServiceNow&location=United+States&radius=25&days=5&refine_by_employment=&refine_by_location_type=only_remote&refine_by_salary=&refine_by_salary_ceil=&refine_by_apply_type=&refine_by_experience_level=junior%2Cmid%2Csenior&seniority_filters_explicitly_set=true&location_types_explicitly_set=true&lk=GkOOYgSk7ZnYh5z5Za34yQ",
        "https://www.glassdoor.com/Job/united-states-servicenow-jobs-SRCH_IL.0,13_IN1_KO14,24.htm?remoteWorkType=1&fromAge=3",
        "https://jooble.org/SearchResult?date=2&loc=2&rgns=United%20States&ukw=servicenow",
        "http://talent.com/jobs?k=ServiceNow&l=United+States&workplace=remote&radius=100&date=3",
    ],
    "Salesforce": [
        "https://www.indeed.com/jobs?q=salesforce&l=Remote&fromage=3&sc=0fcckey%3A8a1faebced32b083%2Cq%3A%3B&from=searchOnDesktopSerp&vjk=40da1d788bab69b0",
        "https://www.dice.com/jobs?countryCode=US&filters.postedDate=THREE&filters.workplaceTypes=Remote&latitude=38.7945952&location=United+States&locationPrecision=Country&longitude=-106.5348379&q=Salesforce&radiusUnit=mi",
        "https://www.ziprecruiter.com/jobs-search?search=Salesforce&location=United+States&radius=25&days=5&refine_by_employment=&refine_by_location_type=only_remote&refine_by_salary=&refine_by_salary_ceil=&refine_by_apply_type=&refine_by_experience_level=junior%2Cmid%2Csenior&seniority_filters_explicitly_set=true&location_types_explicitly_set=true&lk=V7MfmegI7L7E0BQ9XYP4Uw",
        "https://www.glassdoor.com/Job/united-states-salesforce-jobs-SRCH_IL.0,13_IN1_KO14,24.htm?remoteWorkType=1&fromAge=3",
        "https://jooble.org/SearchResult?date=2&loc=2&rgns=United%20States&ukw=salesforce",
        "https://www.talent.com/jobs?k=Salesforce&l=United+States&workplace=remote&radius=100&date=3",
    ],
    "Dynamics": [
        "https://www.indeed.com/jobs?q=Dynamics+365&l=Remote&fromage=3&sc=0kf%3Aattr%28DSQF7%29%3B&from=searchOnDesktopSerp&vjk=0156064cadc6c677",
        "https://www.dice.com/jobs?filters.postedDate=THREE&filters.workplaceTypes=Remote&countryCode=US&latitude=38.7945952&location=United+States&locationPrecision=Country&longitude=-106.5348379&q=Dynamics+365&radiusUnit=mi",
        "https://www.ziprecruiter.com/jobs-search?search=Dynamics+365&location=United+States&radius=25&days=5&refine_by_employment=&refine_by_location_type=only_remote&refine_by_salary=&refine_by_salary_ceil=&refine_by_apply_type=&refine_by_experience_level=junior%2Cmid%2Csenior&seniority_filters_explicitly_set=true&location_types_explicitly_set=true&lk=xtsUQRoO5Koezb0npUsNQQ",
        "https://www.glassdoor.com/Job/united-states-dynamics-365-jobs-SRCH_IL.0,13_IN1_KO14,26.htm?remoteWorkType=1&fromAge=3",
        "https://jooble.org/SearchResult?date=2&loc=2&rgns=United%20States&ukw=dynamics%20365",
        "https://www.talent.com/jobs?k=Dynamics+365&l=United+States&workplace=remote&radius=100&date=3",
    ],
}

# ──────────────────────────────────────────────────────────────────────────────
# Tue – Sun schedule  (all other days)
# ──────────────────────────────────────────────────────────────────────────────
OTHER_LINKS: dict[str, list[str]] = {
    "ML": [
        "https://www.indeed.com/jobs?q=Machine+Learning&l=Remote&fromage=1&sc=0kf%3Aattr%28DSQF7%29%3B&from=searchOnDesktopSerp&vjk=37d67eb7d5499d38",
        "https://www.dice.com/jobs?filters.postedDate=ONE&filters.workplaceTypes=Remote&q=Machine+Learning&location=United+States&radiusUnit=mi&latitude=38.7945952&longitude=-106.5348379&countryCode=US&locationPrecision=Country",
        "https://www.ziprecruiter.com/jobs-search?search=Machine+Learning&location=Remote+%28USA%29&radius=25&days=1&refine_by_employment=&refine_by_location_type=&refine_by_salary=&refine_by_salary_ceil=&refine_by_apply_type=&refine_by_experience_level=junior%2Cmid%2Csenior&seniority_filters_explicitly_set=true&apply_types_explicitly_set=true&employment_types_explicitly_set=true&location_types_explicitly_set=true&radius_explicitly_set=true&lk=YKOOex0ftrOHSkHqpixrsQ",
        "https://www.glassdoor.com/Job/united-states-machine-learning-engineer-jobs-SRCH_IL.0,13_IN1_KO14,39.htm?remoteWorkType=1&fromAge=1",
        "https://jooble.org/SearchResult?date=8&loc=2&rgns=United%20States&ukw=machine%20learning",
        "https://www.talent.com/jobs?k=Machine+Learning&l=United+States&workplace=remote&date=1&radius=100",
        "https://www.adzuna.com/search?f=1&loc=151946&remote_only=1&q=Machine%20Learning",
        "https://remotive.com/remote-jobs?query=Machine+Learning&category=Artificial+Intelligence+%2CEngineering&location=USA&employment-type=full-time%2Ccontract%2Cpart-time",
        "https://himalayas.app/jobs/worldwide/machine-learning?src=adv&type=full-time%2Cpart-time%2Ccontractor%2Ctemporary&view=filters&experience=mid-level%2Csenior%2Cmanager%2Cdirector%2Cexecutive&sort=recent",
        "https://www.remoterocketship.com/?page=1&sort=DateAdded&locations=United+States&jobTitle=Machine+Learning+Engineer%2CData+Scientist%2CAI+Engineer",
        "https://remote100k.com/remote-jobs/data-science",
    ],
    "Data": [
        "https://www.indeed.com/jobs?q=Data+Engineer&l=Remote&fromage=1&sc=0kf%3Aattr%28DSQF7%29%3B&from=searchOnDesktopSerp&vjk=bf7369a247f59fd5",
        "https://www.dice.com/jobs?filters.postedDate=ONE&filters.workplaceTypes=Remote&countryCode=US&latitude=38.7945952&location=United+States&locationPrecision=Country&longitude=-106.5348379&q=data+engineer&radiusUnit=mi",
        "https://www.ziprecruiter.com/jobs-search?search=Data+Engineer&location=United+States&radius=25&days=1&refine_by_employment=&refine_by_location_type=only_remote&refine_by_salary=&refine_by_salary_ceil=&refine_by_apply_type=&refine_by_experience_level=junior%2Cmid%2Csenior&seniority_filters_explicitly_set=true&location_types_explicitly_set=true&lk=Z0_fXsZp2YiUBxDT_VmOiA",
        "https://www.glassdoor.com/Job/united-states-data-engineer-jobs-SRCH_IL.0,13_IN1_KO14,27.htm?remoteWorkType=1&fromAge=1",
        "https://jooble.org/SearchResult?date=8&loc=2&rgns=United%20States&ukw=data%20engineering%20",
        "https://www.talent.com/jobs?k=Data+Engineering&l=United+States&workplace=remote&radius=100&date=1",
        "https://remotive.com/remote-jobs?query=Data+Engineer&category=Engineering%2CData+and+Analytics&location=USA&employment-type=full-time%2Ccontract%2Cpart-time",
        "https://himalayas.app/jobs/worldwide/data-engineering?src=adv&type=full-time%2Cpart-time%2Ccontractor%2Ctemporary&view=filters&experience=mid-level%2Csenior%2Cmanager%2Cdirector%2Cexecutive&sort=recent",
        "https://www.remoterocketship.com/?page=1&sort=DateAdded&locations=United+States&jobTitle=Data+Engineer%2CDatabase+Administrator",
        "https://remote100k.com/remote-jobs/engineering",
    ],
    "ServiceNow": [
        "https://www.indeed.com/jobs?q=ServiceNow&l=Remote&fromage=1&sc=0kf%3Aattr%28DSQF7%29%3B&from=searchOnDesktopSerp&vjk=489d26f44d26f106",
        "https://www.dice.com/jobs?countryCode=US&filters.postedDate=ONE&filters.workplaceTypes=Remote&latitude=38.7945952&location=United+States&locationPrecision=Country&longitude=-106.5348379&q=ServiceNow&radiusUnit=mi",
        "https://www.ziprecruiter.com/jobs-search?search=ServiceNow&location=United+States&radius=25&days=1&refine_by_employment=&refine_by_location_type=only_remote&refine_by_salary=&refine_by_salary_ceil=&refine_by_apply_type=&refine_by_experience_level=junior%2Cmid%2Csenior&seniority_filters_explicitly_set=true&location_types_explicitly_set=true&lk=wFzX5Ytj9fMTp9UFMhA61w",
        "https://www.glassdoor.com/Job/united-states-servicenow-jobs-SRCH_IL.0,13_IN1_KO14,24.htm?remoteWorkType=1&fromAge=1",
        "https://jooble.org/SearchResult?date=8&loc=2&rgns=United%20States&ukw=servicenow",
        "https://www.talent.com/jobs?k=ServiceNow&l=United+States&workplace=remote&radius=100&date=1",
        "https://remotive.com/remote-jobs?query=ServiceNow&category=Engineering%2CProject+Management&location=USA&employment-type=full-time%2Ccontract%2Cpart-time",
        "https://himalayas.app/jobs/worldwide/servicenow?src=adv&type=full-time%2Cpart-time%2Ccontractor%2Ctemporary&view=filters&experience=mid-level%2Csenior%2Cmanager%2Cdirector%2Cexecutive&sort=recent",
        "https://www.remoterocketship.com/?page=1&sort=DateAdded&locations=United+States&jobTitle=ServiceNow",
    ],
    "Salesforce": [
        "https://www.indeed.com/jobs?q=salesforce&l=Remote&fromage=1&sc=0fcckey%3A8a1faebced32b083%2Ckf%3Aattr%28DSQF7%29%2Cq%3A%3B&from=searchOnDesktopSerp&vjk=40da1d788bab69b0",
        "https://www.dice.com/jobs?filters.postedDate=ONE&filters.workplaceTypes=Remote&countryCode=US&latitude=38.7945952&location=United+States&locationPrecision=Country&longitude=-106.5348379&q=Salesforce&radiusUnit=mi",
        "https://www.ziprecruiter.com/jobs-search?search=Salesforce&location=United+States&radius=25&days=1&refine_by_employment=&refine_by_location_type=only_remote&refine_by_salary=&refine_by_salary_ceil=&refine_by_apply_type=&refine_by_experience_level=junior%2Cmid%2Csenior&seniority_filters_explicitly_set=true&location_types_explicitly_set=true&lk=uerpkOtHTzYTCuZcH8tMYg",
        "https://www.glassdoor.com/Job/united-states-salesforce-jobs-SRCH_IL.0,13_IN1_KO14,24.htm?remoteWorkType=1&fromAge=1",
        "http://jooble.org/SearchResult?date=8&loc=2&rgns=United%20States&ukw=salesforce",
        "https://www.talent.com/jobs?k=Salesforce&l=United+States&workplace=remote&radius=100&date=1",
        "https://remotive.com/remote-jobs?query=Salesforce&category=Engineering%2CProject+Management%2CSoftware+Development%2CProduct+Management&location=USA&employment-type=full-time%2Ccontract%2Cpart-time",
        "https://www.remoterocketship.com/?page=1&sort=DateAdded&locations=United+States&jobTitle=Salesforce+Administrator%2CSalesforce+Analyst%2CSalesforce+Consultant%2CSalesforce+Developer",
    ],
    "Dynamics": [
        "https://www.indeed.com/jobs?q=Dynamics+365&l=Remote&fromage=1&sc=0kf%3Aattr%28DSQF7%29%3B&from=searchOnDesktopSerp&vjk=0156064cadc6c677",
        "https://www.dice.com/jobs?countryCode=US&filters.postedDate=ONE&filters.workplaceTypes=Remote&latitude=38.7945952&location=United+States&locationPrecision=Country&longitude=-106.5348379&q=Dynamics+365&radiusUnit=mi",
        "https://www.ziprecruiter.com/jobs-search?search=Dynamics+365&location=United+States&radius=25&days=1&refine_by_employment=&refine_by_location_type=only_remote&refine_by_salary=&refine_by_salary_ceil=&refine_by_apply_type=&refine_by_experience_level=junior%2Cmid%2Csenior&seniority_filters_explicitly_set=true&location_types_explicitly_set=true&lk=ftRUo3GXAJwWLVIHctqexg",
        "https://www.glassdoor.com/Job/united-states-dynamics-365-jobs-SRCH_IL.0,13_IN1_KO14,26.htm?remoteWorkType=1&fromAge=1",
        "https://jooble.org/SearchResult?date=8&loc=2&rgns=United%20States&ukw=dynamics%20365",
        "https://www.talent.com/jobs?k=Dynamics+365&l=United+States&workplace=remote&radius=100&date=1",
        "https://remotive.com/remote-jobs?query=Dynamics+365&category=Engineering%2CProject+Management%2CSoftware+Development%2CProduct+Management%2CMarketing&location=USA&employment-type=full-time%2Ccontract%2Cpart-time",
        "https://himalayas.app/jobs/worldwide/dynamics-365?src=adv&type=full-time%2Cpart-time%2Ccontractor%2Ctemporary&view=filters&experience=mid-level%2Csenior%2Cmanager%2Cdirector%2Cexecutive&sort=recent",
        "https://www.remoterocketship.com/?page=1&sort=DateAdded&locations=United+States&jobTitle=Dynamics+365%2CD365%2CDynamics+CRM+Developer%2CF%26O",
    ],
}


def get_links() -> list[str]:
    """Return the flat list of URLs for today's schedule.

    Monday  → MON_LINKS (all categories combined)
    Tue–Sun → OTHER_LINKS (all categories combined)
    """
    today: int = datetime.date.today().weekday()  # 0 = Monday … 6 = Sunday
    schedule = MON_LINKS if today == 0 else OTHER_LINKS

    day_name: Literal["Monday", "Other"] = "Monday" if today == 0 else "Other"
    print(f"[links] Using {day_name} schedule ({datetime.date.today().strftime('%A')})")

    # Flatten all category lists into one list, preserving insertion order.
    return [url for urls in schedule.values() for url in urls]


# ---------------------------------------------------------------------------
# Backwards-compat: keep LINKS so that any code still importing it directly
# continues to work without changes.
# ---------------------------------------------------------------------------
LINKS: list[str] = get_links()
