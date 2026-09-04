#!/usr/bin/env bash
# LEGACY — DO NOT RUN. This one-off bootstrap script deleted .git and re-initialised the
# repository; it has no place in a repository that already has history. Kept only as a record.
echo "REFUSING: legacy repository bootstrap script (see legacy/frontend-fragments/README.md and CURRENT_STATE.md)" >&2
exit 64

# ---- original content below (inert) ----
# #!/bin/bash
# # Script to push AgentNet repo to GitHub
# # Run this script from the agentnet folder on your computer
# 
# cd "$(dirname "$0")"
# 
# # Remove old .git if exists (from failed attempt)
# rm -rf .git
# 
# # Initialize fresh repo
# git init
# git checkout -b main
# 
# # Create .gitignore
# echo '~$*' > .gitignore
# 
# # Stage all files (excluding temp Word lock files)
# git add .env.example .gitignore docker-compose.yml AgentNet_Code_Review.docx services/
# 
# # Commit
# git commit -m "Initial commit: AgentNet microservices platform
# 
# Includes registry, payment, and worker services with Docker Compose setup."
# 
# # Add remote and push
# git remote add origin https://github.com/vansyson1308/agentnet.git
# git push -u origin main
# 
# echo "Done! Check https://github.com/vansyson1308/agentnet"
