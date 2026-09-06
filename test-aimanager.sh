# Test script to verify AIMANAGER configuration

echo "Testing direct access to service on port 22345:"
curl -s http://localhost:22345/aimanager/models | head -n 3

echo ""
echo "Testing if nginx redirect works (should 200 or similar):"
curl -I http://localhost/aimanager 2>/dev/null | head -n 1

echo ""
echo "Test complete. The service should be accessible at:"
echo "  http://localhost/aimanager   (via nginx redirect)"
echo "  http://localhost:22345/aimanager  (direct access)"