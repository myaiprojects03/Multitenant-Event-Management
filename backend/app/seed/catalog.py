"""Static catalog for the synthetic events: domains, personas and the planted patterns.

The data is designed backwards from the demo questions (see docs/PLAN.md, Phase 1).
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Persona:
    key: str
    email: str
    full_name: str
    role: str  # attendee | exhibitor | organizer
    label: str
    exhibitor: str | None = None  # exhibitor name for exhibitor staff
    events: tuple[str, ...] = ()  # event keys
    profile: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EventSpec:
    key: str
    name: str
    slug: str
    kind: str
    venue: str
    # (day offset from anchor date, number of parallel sessions per slot)
    days: tuple[tuple[int, int], ...]
    slots: tuple[str, ...]
    n_attendees: int
    n_exhibitors: int
    n_speakers: int
    tracks: dict[str, list[str]]  # track -> interest tags
    rooms: tuple[tuple[str, str, int], ...]  # (name, floor, capacity); labs contain "Lab"
    industries: tuple[str, ...]
    exhibitor_categories: dict[str, list[str]]  # category -> tags
    exhibitor_names: tuple[str, ...]
    wifi: str
    fixed_exhibitors: dict[str, tuple[str, str]] = field(default_factory=dict)  # name->(cat,booth)
    dropoff_title: str | None = None
    overbooked_title: str | None = None
    spike_exhibitor: str | None = None


@dataclass(frozen=True)
class TenantSpec:
    key: str
    name: str
    slug: str
    email_domain: str
    events: tuple[EventSpec, ...]


TITLE_PATTERNS = (
    "{topic} in Practice",
    "Scaling {topic}: Lessons from the Field",
    "The Future of {topic}",
    "Building Trust in {topic}",
    "From Pilot to Production: {topic}",
    "{topic} on a Budget",
    "What Nobody Tells You About {topic}",
    "{topic}: Case Studies and Pitfalls",
)
PANEL_PATTERN = "Panel: Where {topic} Goes Next"
WORKSHOP_PATTERN = "Hands-on Workshop: {topic}"
KEYNOTE_TITLE = "Opening Keynote: {event}"

SENIORITY = ("junior", "mid", "senior", "manager", "director", "executive")
PERSONA_TYPES = ("buyer", "seller", "mentor", "mentee", "researcher", "investor", "peer")
LOOKING_FOR = (
    "vendors", "partners", "investors", "mentorship", "hiring", "customers",
    "research collaborators", "peer exchange",
)  # fmt: skip
JOB_TITLES = {
    "junior": ("Analyst", "Associate", "Coordinator"),
    "mid": ("Engineer", "Specialist", "Consultant"),
    "senior": ("Senior Engineer", "Senior Consultant", "Lead Specialist"),
    "manager": ("Product Manager", "Program Manager", "Engineering Manager"),
    "director": ("Director of Innovation", "Director of Operations", "Director of IT"),
    "executive": ("Chief Technology Officer", "VP Strategy", "Chief Medical Information Officer"),
}

# --- tenant A: healthcare ---------------------------------------------------------------------

HEALTH_TRACKS = {
    "Clinical AI": ["radiology AI", "clinical decision support", "medical imaging"],
    "Digital Health & Telemedicine": ["telehealth", "patient engagement", "wearables"],
    "Remote Care": ["remote patient monitoring", "wearables", "population health"],
    "Health Data & Interoperability": ["FHIR", "EHR interoperability", "health data privacy"],
    "Hospital Operations": ["revenue cycle", "value-based care", "health cybersecurity"],
}
HEALTH_ROOMS = (
    ("Auditorium", "Level 1", 800),
    ("Hall A", "Level 1", 300),
    ("Room 101", "Level 1", 120),
    ("Room 102", "Level 1", 120),
    ("Workshop Lab 1", "Level 2", 40),
    ("Workshop Lab 2", "Level 2", 40),
)
HEALTH_INDUSTRIES = (
    "Hospital & Health System", "Medical Devices", "Pharma & Life Sciences", "Health IT",
    "Payer & Insurance", "Research & Academia", "Digital Health Startup",
)  # fmt: skip
HEALTH_EXHIBITOR_CATEGORIES = {
    "Medical Imaging": ["radiology AI", "medical imaging", "clinical decision support"],
    "Telehealth Platforms": ["telehealth", "patient engagement"],
    "EHR & Interoperability": ["FHIR", "EHR interoperability"],
    "Remote Monitoring": ["remote patient monitoring", "wearables"],
    "Healthcare Analytics": ["population health", "value-based care", "revenue cycle"],
    "Health Cybersecurity": ["health cybersecurity", "health data privacy"],
}
HEALTH_EXHIBITOR_NAMES = (
    "VitalLink", "CareBridge", "PulseNet Health", "Medisync", "HealthGrid", "NovaCare Systems",
    "ClearPath Clinical", "Lumen Diagnostics", "OrbitMed", "TrueNorth Health IT", "Apex Imaging",
    "BrightWard", "Helix Analytics", "Sentinel Health Security", "CareCloud Labs",
    "Everwell Devices", "Quantum Radiology", "OpenChart", "Meridian Telehealth", "SafeHarbor Data",
    "PrimeVitals", "Cascade Health", "Northstar Monitoring", "Keystone EHR", "Aurora Clinical AI",
    "BlueRiver Health", "Insight Imaging", "WellSpring Digital", "Synapse Health",
    "Beacon Remote Care", "Harbor Health Analytics", "Pinnacle Devices", "Clarity Care",
)  # fmt: skip

HEALTHTECH_SUMMIT = EventSpec(
    key="summit",
    name="HealthTech Summit 2026",
    slug="healthtech-summit-2026",
    kind="healthcare",
    venue="Harborview Convention Center, Boston",
    days=((-2, 3), (-1, 3), (0, 1), (1, 1)),
    slots=("09:00", "10:30", "13:00", "14:30", "16:00"),
    n_attendees=400,
    n_exhibitors=30,
    n_speakers=32,
    tracks=HEALTH_TRACKS,
    rooms=HEALTH_ROOMS,
    industries=HEALTH_INDUSTRIES,
    exhibitor_categories=HEALTH_EXHIBITOR_CATEGORIES,
    exhibitor_names=HEALTH_EXHIBITOR_NAMES,
    wifi="Network HTS-Guest, password Summit2026!",
    fixed_exhibitors={
        "MedScan": ("Medical Imaging", "B12"),
        "CardioVue": ("Medical Imaging", "B14"),
    },
    dropoff_title="The Future of Remote Patient Monitoring",
    overbooked_title="Hands-on Workshop: Building FHIR Integrations",
    spike_exhibitor="MedScan",
)

CLINICAL_FORUM = EventSpec(
    key="forum",
    name="Clinical AI Forum (Spring)",
    slug="clinical-ai-forum-spring",
    kind="healthcare",
    venue="Riverside Medical Campus, Chicago",
    days=((-60, 2), (-59, 2)),
    slots=("10:00", "14:00"),
    n_attendees=60,
    n_exhibitors=5,
    n_speakers=8,
    tracks={k: HEALTH_TRACKS[k] for k in ("Clinical AI", "Health Data & Interoperability")},
    rooms=(("Main Room", "Ground", 150), ("Seminar Room", "Ground", 60)),
    industries=HEALTH_INDUSTRIES,
    exhibitor_categories=HEALTH_EXHIBITOR_CATEGORIES,
    exhibitor_names=HEALTH_EXHIBITOR_NAMES,
    wifi="Network RMC-Visitor, password Forum-Spring",
)

# --- tenant B: tech trade show ----------------------------------------------------------------

TECH_TRACKS = {
    "Cloud & Infrastructure": ["kubernetes", "cloud cost optimization", "platform engineering"],
    "AI/ML Engineering": ["LLM applications", "MLOps", "vector databases"],
    "Security": ["zero trust", "application security", "identity management"],
    "Developer Experience": ["developer productivity", "CI/CD", "observability"],
    "Data Platforms": ["data engineering", "streaming", "data governance"],
}
TECH_ROOMS = (
    ("Main Stage", "Hall 1", 1000),
    ("Theater A", "Hall 1", 250),
    ("Theater B", "Hall 2", 150),
    ("Theater C", "Hall 2", 150),
    ("Builder Lab 1", "Hall 3", 40),
    ("Builder Lab 2", "Hall 3", 40),
)
TECH_INDUSTRIES = (
    "SaaS", "Financial Services", "Retail & E-commerce", "Telecom", "Public Sector",
    "Manufacturing", "Consulting", "Developer Tools Startup",
)  # fmt: skip
TECH_EXHIBITOR_CATEGORIES = {
    "Cloud Infrastructure": ["kubernetes", "platform engineering", "cloud cost optimization"],
    "AI Platforms": ["LLM applications", "MLOps", "vector databases"],
    "Security": ["zero trust", "application security", "identity management"],
    "DevOps Tooling": ["CI/CD", "observability", "developer productivity"],
    "Data Infrastructure": ["data engineering", "streaming", "data governance"],
}
TECH_EXHIBITOR_NAMES = (
    "StackPilot", "NimbusOps", "Gridline Data", "ByteHarbor", "Vaultic", "Shipyard CI",
    "TraceLoop", "Kubevault", "DataRiver", "PromptWorks", "Zerotrace", "Cobalt Cloud", "Flowstate",
    "Northwind Observability", "Ironclad Identity", "Quasar ML", "Pipeforge", "Lattice DB",
    "Signal Stack", "Helios Compute", "Brightline Security", "Mosaic Data", "Tensorly",
    "DeployDeck", "Archway APIs", "Crestline Cloud", "Echo Streams", "Vectorly", "Sparkline Dev",
    "Oakridge Systems", "Bluefin Security", "Summit Data Co",
)  # fmt: skip

DEVWORLD_EXPO = EventSpec(
    key="expo",
    name="DevWorld Expo 2026",
    slug="devworld-expo-2026",
    kind="tech",
    venue="Bayfront Expo Hall, San Francisco",
    days=((-2, 3), (-1, 3), (0, 1), (1, 1)),
    slots=("09:00", "10:30", "13:00", "14:30", "16:00"),
    n_attendees=300,
    n_exhibitors=30,
    n_speakers=32,
    tracks=TECH_TRACKS,
    rooms=TECH_ROOMS,
    industries=TECH_INDUSTRIES,
    exhibitor_categories=TECH_EXHIBITOR_CATEGORIES,
    exhibitor_names=TECH_EXHIBITOR_NAMES,
    wifi="Network DevWorld-Free, password ShipIt2026",
    fixed_exhibitors={"CloudForge": ("Cloud Infrastructure", "C07")},
    dropoff_title="Scaling Kubernetes: Lessons from the Field",
    overbooked_title="Hands-on Workshop: LLM Applications",
    spike_exhibitor="CloudForge",
)

TENANTS = (
    TenantSpec(
        key="northwind",
        name="Northwind Health Events",
        slug="northwind-health",
        email_domain="northwind-health.example.com",
        events=(HEALTHTECH_SUMMIT, CLINICAL_FORUM),
    ),
    TenantSpec(
        key="apex",
        name="Apex Expo Group",
        slug="apex-expo",
        email_domain="apex-expo.example.com",
        events=(DEVWORLD_EXPO,),
    ),
)

PERSONAS: dict[str, tuple[Persona, ...]] = {
    "northwind": (
        Persona(
            key="sara",
            email="sara@northwind-health.example.com",
            full_name="Sara Ahmed",
            role="attendee",
            label="Sara, attendee (opted in to networking and exhibitor matching)",
            events=("summit",),
            profile=dict(
                job_title="Clinical Informatics Lead",
                company="St. Aurelia Hospital",
                industry="Hospital & Health System",
                seniority="manager",
                persona_type="buyer",
                interests=["radiology AI", "clinical decision support", "FHIR"],
                looking_for=["vendors", "peer exchange"],
                offering=["clinical workflow expertise"],
                goals="Evaluate imaging AI vendors and meet other informatics leads.",
                networking_opt_in=True,
                exhibitor_matching_opt_in=True,
            ),
        ),
        Persona(
            key="omar",
            email="omar@northwind-health.example.com",
            full_name="Omar Haddad",
            role="attendee",
            label="Omar, attendee (opted out of networking and matching)",
            events=("summit",),
            profile=dict(
                job_title="Radiologist",
                company="Lakeside Imaging Group",
                industry="Hospital & Health System",
                seniority="senior",
                persona_type="peer",
                interests=["radiology AI", "medical imaging"],
                looking_for=["peer exchange"],
                offering=["radiology expertise"],
                goals="Attend talks only.",
                networking_opt_in=False,
                exhibitor_matching_opt_in=False,
            ),
        ),
        Persona(
            key="ali",
            email="ali@northwind-health.example.com",
            full_name="Ali Raza",
            role="organizer",
            label="Ali, organizer (both Northwind events)",
            events=("summit", "forum"),
        ),
        Persona(
            key="maya",
            email="maya@medscan.example.com",
            full_name="Maya Chen",
            role="exhibitor",
            label="Maya, exhibitor staff at MedScan",
            exhibitor="MedScan",
            events=("summit",),
        ),
        Persona(
            key="tom",
            email="tom@cardiovue.example.com",
            full_name="Tom Becker",
            role="exhibitor",
            label="Tom, exhibitor staff at CardioVue (MedScan's competitor)",
            exhibitor="CardioVue",
            events=("summit",),
        ),
    ),
    "apex": (
        Persona(
            key="grace",
            email="grace@apex-expo.example.com",
            full_name="Grace Lin",
            role="organizer",
            label="Grace, organizer (DevWorld Expo)",
            events=("expo",),
        ),
        Persona(
            key="leo",
            email="leo@apex-expo.example.com",
            full_name="Leo Park",
            role="attendee",
            label="Leo, attendee (DevWorld Expo)",
            events=("expo",),
            profile=dict(
                job_title="Staff Platform Engineer",
                company="Fintrail",
                industry="Financial Services",
                seniority="senior",
                persona_type="buyer",
                interests=["kubernetes", "observability", "LLM applications"],
                looking_for=["vendors", "peer exchange"],
                offering=["platform engineering experience"],
                goals="Find an observability vendor and compare notes on internal platforms.",
                networking_opt_in=True,
                exhibitor_matching_opt_in=True,
            ),
        ),
        Persona(
            key="nina",
            email="nina@cloudforge.example.com",
            full_name="Nina Patel",
            role="exhibitor",
            label="Nina, exhibitor staff at CloudForge",
            exhibitor="CloudForge",
            events=("expo",),
        ),
    ),
}
