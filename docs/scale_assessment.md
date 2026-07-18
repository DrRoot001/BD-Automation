# Honest Scalability & Kubernetes Assessment: BD-Automator-Agent

We performed a deep-dive analysis of your current VPS resources (8 CPU Cores, 24 GB RAM) against the requirements of running 270 candidate profiles and 50 BD users. Below is our honest technical assessment and options for scaling.

---

## 📊 The Math: Resource Consumption of Browser Automation

Browser automation (Playwright/Chromium) is highly resource-intensive. For a single active application job:
* **Memory:** A single Chromium browser instance consumes **500MB – 800MB RAM**.
* **CPU:** A single active automation loop consumes **0.5 – 1.0 CPU Core** (rendering web pages, filling forms, solving selectors).

### Scenario A: Worst-Case Concurrency (No Queue Limits)
If all 50 BD users trigger an application run at the same time:
* **Memory Needed:** $50 \text{ runs} \times 750\text{MB} = 37.5\text{ GB RAM}$ (Only for browsers)
* **CPU Needed:** $50 \text{ runs} \times 0.5\text{ Cores} = 25\text{ CPU Cores}$

> [!CAUTION]
> **Result on the current VPS:** Running 50 concurrent browsers on your 8-Core/24GB VPS will saturate the CPU (load average > 50), exhaust the RAM, trigger the Linux OOM (Out of Memory) Killer, and crash the database and backend.

---

## 🛠️ Option 1: Scale on the Current VPS (Docker Compose + Queue Concurrency)
**We can fully support all 50 users on your current VPS** without buying new hardware, by using the **queue limits** already built into Celery.

Instead of letting 50 browsers run at once, we configure the Celery worker concurrency to a safe maximum (e.g., **4 concurrent runs**):
* **Memory Peak:** $4 \text{ runs} \times 750\text{MB} = 3.0\text{ GB RAM}$.
* **CPU Peak:** $4 \text{ runs} \times 0.5\text{ Cores} = 2.0\text{ CPU Cores}$.
* **How it works:** When users click "Apply", the tasks enter the Redis queue. The 4 workers pull tasks one-by-one.
* **The Trade-off:** Users might experience a queue delay of 1-3 minutes before their browser session starts if multiple runs are triggered.

> [!TIP]
> **Verdict:** Highly cost-effective. The current VPS is more than powerful enough if we keep concurrency controlled.

---

## ☸️ Option 2: Migrate to Kubernetes (Multi-Node Cloud Cluster)
If you decide to implement Kubernetes, **doing it on the current single VPS (using MicroK8s or K3s) will not help.** Kubernetes is a scheduler; it cannot create more RAM or CPU than the physical machine has. In fact, Kubernetes adds **1.5GB – 2GB of RAM overhead** just to run its control plane.

To make Kubernetes useful, we must deploy a **Managed Cloud Cluster** (e.g., AWS EKS, Google GKE, or DigitalOcean Kubernetes):

### Pros:
1. **Dynamic Scaling:** You can use **KEDA (Kubernetes Event-driven Autoscaling)**. When the queue size goes from 0 to 50, K8s automatically provisions new node instances, spins up 50 worker pods, processes all applications in parallel, and then deletes the nodes to save money.
2. **High Availability:** If a worker crashes, K8s kills the container and starts a fresh one instantly.
3. **No Queue Delay:** All 50 users can run processes concurrently without waiting.

### Cons:
1. **High Cost:** Running a multi-node cluster with autoscaling nodes starts at **$150 - $350+/month** minimum.
2. **Maintenance Complexity:** Requires managing container registries, DNS ingress routing, cloud load balancers, and persistent cloud file shares (AWS EFS / GCP Filestore).

---

## 📋 Recommendations & Strategy

| Feature | Option 1: VPS + Celery Queue Limit (Docker Compose) | Option 2: Cloud Kubernetes (AWS EKS / GKE) |
| :--- | :--- | :--- |
| **Max Concurrent Runs** | 4 - 6 active browsers | 50+ active browsers |
| **User Experience** | Fast, but queued tasks wait 1-3 mins | Instant execution, zero wait |
| **Monthly Infrastructure Cost** | **$0** (using your current VPS) | **$150 - $400+/month** |
| **Setup Complexity** | Very Low (Done) | High (Needs cluster setup & deployment pipeline) |

### Our Advice
Start with **Option 1 (VPS + Concurrency Queue)**. It is already fully deployed on your VPS. We can adjust the concurrency limit on the Celery worker to keep CPU/RAM usage perfectly stable. 

If your candidate load increases to 1,000+ profiles and users complain about waiting in queues, we can transition the setup to **Cloud Kubernetes** without changing any application code.
