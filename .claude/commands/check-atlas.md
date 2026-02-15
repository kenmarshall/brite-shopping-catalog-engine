Check MongoDB Atlas cluster status and connectivity.

Usage: /check-atlas

Steps:
1. Check cluster status: `atlas clusters list --projectId 66fdd84e2a49b44f750e0147 -o json 2>&1 | python3 -c "import sys,json; d=json.load(sys.stdin); c=d['results'][0]; print(f'Cluster: {c[\"name\"]}, State: {c[\"stateName\"]}, Paused: {c[\"paused\"]}, Version: {c[\"mongoDBVersion\"]}')"`
2. Test local connectivity: `cd /Users/kennethmarshall/dev/brite_shopping/brite-shopping-catalog-engine && .venv/bin/python -c "from agent.db.mongo import MongoService; ms = MongoService(); print(f'Products: {ms.products.count_documents({})}')"`
3. Test API connectivity: `curl -s --max-time 15 https://brite-shopping-api.onrender.com/health`
4. If cluster is paused: it's an M0 free tier — user must resume from Atlas console (atlas CLI can't resume M0)
5. If API fails but local works: check Atlas IP access list with `atlas accessLists list --projectId 66fdd84e2a49b44f750e0147`

Atlas project ID: 66fdd84e2a49b44f750e0147
Cluster name: brite-shopping-dev
