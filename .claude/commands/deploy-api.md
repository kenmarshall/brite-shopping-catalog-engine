Deploy the API to Render by committing and pushing changes.

Usage: /deploy-api

Steps:
1. Check for uncommitted changes in `/Users/kennethmarshall/dev/brite_shopping/brite_shopping_api` with `git status`
2. If there are changes, stage, commit with a descriptive message, and push to main
3. Render auto-deploys from main — wait ~2 minutes then verify with: `curl -s https://brite-shopping-api.onrender.com/health`
4. If health check fails, check `curl -sv https://brite-shopping-api.onrender.com/health 2>&1 | tail -20` for details
5. Report deploy status

Note: Render free tier can take 2-5 minutes to redeploy. The service sleeps after inactivity — first request after sleep takes ~30-60s.
