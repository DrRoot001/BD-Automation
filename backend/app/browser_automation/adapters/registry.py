from .ashby import AshbyAdapter
from .base import BasePlatformAdapter
from .builtin import BuiltInAdapter
from .careerplug import CareerPlugAdapter
from .careerspage import CareersPageAdapter
from .dice import DiceAdapter
from .generic import GenericFormAdapter
from .glassdoor import GlassdoorAdapter
from .greenhouse import GreenhouseAdapter
from .himalayas import HimalayasAdapter
from .icims import ICIMSAdapter
from .indeed import IndeedAdapter
from .jobvite import JobviteAdapter
from .lever import LeverAdapter
from .linkedin import LinkedInEasyApplyAdapter
from .remote100k import Remote100KAdapter
from .remoterocketship import RemoteRocketshipAdapter
from .smartrecruiters import SmartRecruitersAdapter
from .talent import TalentAdapter
from .teamtailor import TeamTailorAdapter
from .workday import WorkdayAdapter
from .ziprecruiter import ZipRecruiterAdapter


ADAPTER_REGISTRY: dict[str, type[BasePlatformAdapter]] = {
    "greenhouse": GreenhouseAdapter,
    "lever": LeverAdapter,
    "ashby": AshbyAdapter,
    "workday": WorkdayAdapter,
    "linkedin": LinkedInEasyApplyAdapter,
    "remoterocketship": RemoteRocketshipAdapter,
    "remote100k": Remote100KAdapter,
    # Careers Page — hosted employer ATS (Manatal). Reached directly or via an
    # aggregator external-apply redirect.
    "careerspage": CareersPageAdapter,
    "careers-page": CareersPageAdapter,
    "manatal": CareersPageAdapter,
    "careerplug": CareerPlugAdapter,
    # TeamTailor — hosted ATS white-labelled onto employer career domains.
    # "recruitee" is an alias: handoff v7 mislabelled this ATS as Recruitee, and
    # both are Rails ATSes with near-identical candidate[...] markup, so any
    # upstream source='recruitee' still routes here.
    "teamtailor": TeamTailorAdapter,
    "recruitee": TeamTailorAdapter,
    "icims": ICIMSAdapter,
    "indeed": IndeedAdapter,
    "smartrecruiters": SmartRecruitersAdapter,
    "jobvite": JobviteAdapter,
    "dice": DiceAdapter,
    "talent": TalentAdapter,
    "builtin": BuiltInAdapter,
    "glassdoor": GlassdoorAdapter,
    "ziprecruiter": ZipRecruiterAdapter,
    "himalayas": HimalayasAdapter,
    # Pure aggregators — the RemoteRocketship passthrough (scrape the ATS
    # link off the listing, delegate to the inner ATS adapter) handles them.
    "remoteok": RemoteRocketshipAdapter,
    "adzuna": RemoteRocketshipAdapter,
    "hiringcafe": RemoteRocketshipAdapter,
    "thehiring.cafe": RemoteRocketshipAdapter,
    "thehiringcafe": RemoteRocketshipAdapter,
    "generic": GenericFormAdapter,
}


def get_adapter(platform: str) -> BasePlatformAdapter:
    key = (platform or "").lower().strip()
    # Normalize hyphens/underscores so "remote_rocketship" and "remote-rocketship"
    # also resolve. We only normalize for the substring lookup, not the
    # exact-key dict get, to keep canonical keys unambiguous.
    normalized = key.replace("-", "").replace("_", "")
    # Detect ATS from URL substring when caller only has the URL
    cls = ADAPTER_REGISTRY.get(key)
    if cls is None and key:
        if "remote100k" in normalized:
            cls = Remote100KAdapter
        elif "remoterocketship" in normalized:
            cls = RemoteRocketshipAdapter
        elif "careers-page.com" in key or "careerspage" in normalized or "manatal" in normalized:
            cls = CareersPageAdapter
        elif "careerplug.com" in key or "careerplug" in normalized:
            cls = CareerPlugAdapter
        elif "teamtailor" in normalized or "recruitee" in normalized:
            cls = TeamTailorAdapter
        elif "myworkdayjobs" in key:
            cls = WorkdayAdapter
        elif "lever.co" in key:
            cls = LeverAdapter
        elif "ashbyhq" in key:
            cls = AshbyAdapter
        elif "greenhouse" in key:
            cls = GreenhouseAdapter
        elif "icims.com" in key or "icims" in normalized:
            cls = ICIMSAdapter
        elif "linkedin" in key:
            cls = LinkedInEasyApplyAdapter
        elif "smartrecruiters" in normalized:
            cls = SmartRecruitersAdapter
        elif "jobvite" in normalized:
            cls = JobviteAdapter
        elif "indeed.com" in key or "smartapply" in key:
            cls = IndeedAdapter
        elif "dice.com" in key or "dice" in normalized:
            cls = DiceAdapter
        elif "talent.com" in key or "talent" in normalized:
            cls = TalentAdapter
        elif "builtin.com" in key or "builtin" in normalized:
            cls = BuiltInAdapter
        elif "glassdoor" in normalized:
            cls = GlassdoorAdapter
        elif "ziprecruiter" in normalized:
            cls = ZipRecruiterAdapter
        elif "himalayas.app" in key or "himalayas" in normalized:
            cls = HimalayasAdapter
        # Aggregators → RemoteRocketship passthrough. "hiringcafe" only
        # matches after dot-stripping ("thehiring.cafe" → "thehiringcafe").
        elif "remoteok.com" in key or "remoteok" in normalized:
            cls = RemoteRocketshipAdapter
        elif "adzuna" in normalized:
            cls = RemoteRocketshipAdapter
        elif "thehiring.cafe" in key or "hiringcafe" in normalized.replace(".", ""):
            cls = RemoteRocketshipAdapter
    return (cls or GenericFormAdapter)()
