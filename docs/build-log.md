# Build log

Real obstacles hit while building, and how each was solved. Feeds the form question
"Build Challenges & Technical Obstacles". Newest at the bottom. Keep entries honest and short.

## 2026-09-03

- Research doc recommended "submit the form now, polish later". The form requires the repo URL and
  video link and is marked final-on-submit, so the plan was reversed: build first, submit once.
- The 2-week plan had to fit ~16 hours: cut to one merchant, no UI, no webhooks, polling instead.
- No paid LLM credits: switched the agent to an OpenAI-compatible client so Gemini's free tier and a
  local Ollama model both work through one code path. The gate does not care which model proposes.
- Razorpay test mode allows only 30 Payment Links per account, so all development and tests run on
  a fake executor; real calls are reserved for the smoke test and the recorded runs.
