# shortener
Small Python URL Shortener

# Usage

## Add a link
```
curl -s -X POST https://xameco.net/add \
  -H "X-API-Key: your-secret" \
  -H "Content-Type: application/json" \
  -d '{"slug":"vr","target":"https://velociraptor.app"}'
```

## List all links
```
curl -s https://xameco.net/list -H "X-API-Key: your-secret" | jq
```

## Delete a link
```
curl -s -X DELETE https://xameco.net/remove/vr -H "X-API-Key: your-secret"
```

## Health check (no auth needed)
```
curl -s https://xameco.net/healthz
```
