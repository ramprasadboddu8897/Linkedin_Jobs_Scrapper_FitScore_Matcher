# LinkedIn Job Scraper + Resume Matcher — Deployment Guide

This guide explains how to deploy this application (Python/Flask backend + Vanilla JS frontend) to **Render** for free hosting.

---

## Architecture of Deployment
Render hosts the Python Flask server as a **Web Service** on its free tier. The Flask server serves the static frontend (`index.html`, `index.css`, `app.js`) from the root directory and handles the resume parsing, matching, and scraping API endpoints.

---

## Step 1: Initialize Git and Push to GitHub

Since Render connects directly to GitHub, you first need to push your code to a repository.

1. Open your terminal in the project directory (`D:\AI_Research`).
2. Initialize Git, add files, and commit:
   ```bash
   git init
   git add .
   git commit -m "Configure cloud deployment and target companies highlight"
   ```
3. Create a **Private or Public** repository on [GitHub](https://github.com).
4. Run the commands provided by GitHub to link your local repository and push:
   ```bash
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO_NAME.git
   git push -u origin main
   ```

*(Note: Sensitive configurations like `config.json` and local cache databases in `data/` are automatically ignored by `.gitignore` so they won't be exposed on GitHub).*

---

## Step 2: Deploy to Render (Free Tier)

1. Sign up or log in at [Render.com](https://render.com).
2. Click the **New +** button in the top right and select **Web Service**.
3. Link your GitHub account and select your repository (`YOUR_REPO_NAME`).
4. Set the following configuration details:
   - **Name**: `linkedin-job-matcher` (or any custom name)
   - **Environment / Runtime**: `Python`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn server:app`
   - **Instance Type**: **Free**

---

## Step 3: Configure Environment Variables

Render uses environment variables to securely store credentials like the Apify Token.

1. On the Render deployment dashboard, go to your service's **Environment** tab.
2. Click **Add Environment Variable** and enter the following key-value pairs:
   - `APIFY_TOKEN`: `YOUR_APIFY_API_TOKEN` (Get this from your Apify console under Settings > Integrations)
   - `APIFY_ACTOR_ID`: `apify/linkedin-jobs-scraper` (Default)
   - `DEFAULT_KEYWORDS`: `ServiceNow Technical Support Engineer` (Optional default search key)
   - `DEFAULT_LOCATION`: `India` (Optional default location)
3. Click **Save Changes**.

---

## Step 4: Access Your Application

Render will build and deploy the application (this takes 2-3 minutes). Once complete, you will receive a public URL (e.g., `https://linkedin-job-matcher.onrender.com`).

- Open the URL in any browser.
- **Save Scraper Credits**: Drop your `.docx` resume and click **Reload Cache** to immediately score cached jobs or upload new resumes without scraping!
- **Recruiter Network**: The application automatically highlights target companies loaded from `india_remote_companies.md` with glowing cyan borders and badges, including quick links to their careers pages.

---

### Why not Netlify?
Netlify is a static-only hosting provider. While it can host HTML/JS frontends, it cannot run the persistent Python backend needed for Scikit-Learn TF-IDF matching and Word Doc parsing. Render runs the full Python stack seamlessly and for free.
