#!/bin/bash
# Script to push AgentNet repo to GitHub
# Run this script from the agentnet folder on your computer

cd "$(dirname "$0")"

# Remove old .git if exists (from failed attempt)
rm -rf .git

# Initialize fresh repo
git init
git checkout -b main

# Create .gitignore
echo '~$*' > .gitignore

# Stage all files (excluding temp Word lock files)
git add .env.example .gitignore docker-compose.yml AgentNet_Code_Review.docx services/

# Commit
git commit -m "Initial commit: AgentNet microservices platform

Includes registry, payment, and worker services with Docker Compose setup."

# Add remote and push
git remote add origin https://github.com/vansyson1308/agentnet.git
git push -u origin main

echo "Done! Check https://github.com/vansyson1308/agentnet"
