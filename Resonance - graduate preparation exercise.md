# Case Study: Exchange4Health

*Take-home build brief*

## 1 At a glance

Exchange4Health is a software company that runs the digital back office for home care in the US. Its platform connects the people who pay for care, the agencies that deliver it, and the caregivers who visit patients at home.

| | |
|---|---|
| **Customer type** | B2B2C: sells to payers and providers; caregivers and families use it day to day |
| **Focus** | Home and community-based services |
| **Annual payments processed** | $38 billion |
| **Visits confirmed per year** | 485 million |
| **Caregivers clocking in monthly** | 2.7 million |
| **Customers** | 32,000+ providers, 180+ health insurers, government healthcare programmes |

## 2 The problem: home care is a complex three-sided ecosystem

The government pays for millions of older adults and people with disabilities to receive care at home instead of in a facility. Three groups have to coordinate, and each has a different need:

- **Payers** (state healthcare programmes and the managed care organisations they contract to procure care services on their behalf) need proof that paid-for care actually happened, and want to keep costs and fraud down.
- **Providers** (home care agencies) deliver care for a particular population of members (patients). They coordinate caregivers' schedules, document the visits delivered, and bill payers correctly for the volume and type of care delivered. Each payer has its own rules, which are highly complex and depend on the patient's specific conditions, so payment reconciliation is difficult and billing is often slow and error-prone.
- **Caregivers** (the aides who visit patients) need to know which patients they should visit and the care plans they must follow, and they must log their hours correctly to get paid on time and in full.

Before platforms like Exchange4Health, much of this ecosystem ran on paper timesheets, phone calls and spreadsheets.

Recent regulatory changes have been a strong tailwind for Exchange4Health. Most states now enforce Electronic Visit Verification (EVV): every visit must be digitally logged with key metadata recorded, including who delivered the visit, where it took place, how long it lasted and what services were delivered. The Exchange4Health mobile app includes geolocation and telephony features so caregivers can clock in and out of their visits, allowing providers to comply with EVV regulations with ease.

## 3 What Exchange4Health provides

Exchange4Health offers one cloud platform (SaaS) that all three groups log into. What each group gets:

| User | What they use it for | Examples of products |
|---|---|---|
| Payers (health plans, states) | Monitor trends in care delivery, compliance and cost across their networks | Business intelligence dashboards, programme integrity tools, provider network management, value-based care |
| Providers (agencies) | Run daily operations and get paid | Scheduling, clinical documentation, billing and claims, payroll, outsourced revenue cycle services |
| Caregivers | Find shifts, clock in and out, log care | Exchange4Health+ mobile app |
| Everyone | Meet compliance rules | EVV, including a state-sponsored EVV portal |

The company's key value proposition is that it is the only product connecting the whole home care ecosystem in one place, so a visit logged by a caregiver flows straight through to the agency's bill and the payer's reporting.

## 4 How the model works

Money flows down from payers to providers to caregivers, while visit data flows back up. Exchange4Health sits across all three, so one clock-in becomes a scheduled shift, an EVV record, a claim and a payer report. The company sells to payers and providers; caregivers use the mobile app as part of their agency's setup. Its pricing model is not published on the website.

## 5 Growth trajectory

Exchange4Health has grown quickly over the last few years and is now the dominant provider in more than 30 US states.

## 6 The opportunity: getting claims right the first time

Exchange4Health sees AI as a key driver of future growth. After a recent internal workshop, the leadership team has made claims its top priority.

**The idea.** Build an AI assistant for agencies' billing teams. It would read each payer's rules, check a claim against them before submission, predict which claims will be denied, and suggest the fix.

**Who benefits.** Providers, who today deal with different rules from every payer and lose time and cash to rejected claims.

**Data available.** Claims history with outcomes (paid, denied, reason), plus payer rule documents and contracts.

## 7 The ask

Leadership has framed this as an assistant. You're free to be more ambitious if you think the case supports it. Using Claude Code, we'd like you to:

1. **Frame the problem.** Decide which part of claims you'll tackle, who it's for and what good looks like. Keep the scope to something you can build well in the time.
2. **Design the system.** Sketch the main components, what the AI does versus what's rules-based, what data and context it needs, and where a human stays in the loop.
3. **Build it.** A working prototype that runs on the synthetic data provided.
4. **Check it works.** Show how you'd know whether it's right, for example by testing it against the claims history.

Alongside this brief, you are receiving a synthetic data pack: including ~3,800 past claims with outcomes, ~550 claims not yet submitted, the EVV visit and authorization records behind them, and each payer's provider manual and bulletins. Its README explains the files.

**On the day** you'll have about 10 minutes to present your solution, and we'll discuss it together. Please cover how you approached the problem, how you designed the system, what you built (with a live demo), and how you worked with AI to build your solution.
