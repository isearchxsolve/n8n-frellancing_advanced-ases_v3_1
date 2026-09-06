#!/bin/bash
# ASES - Operations & Monitoring Script

ASES_DIR="${HOME}/ases"
CMD="${1:-status}"

case "$CMD" in
    status)
        echo "=== ASES Service Status ==="
        cd "$ASES_DIR/docker" 2>/dev/null && docker compose ps || echo "Not deployed"
        echo ""
        echo "=== Disk Usage ==="
        df -h / | tail -1
        echo ""
        echo "=== Memory ==="
        free -h | grep Mem
        echo ""
        echo "=== SSL Expiry ==="
        sudo certbot certificates 2>/dev/null || echo "No certificates"
        ;;

    logs)
        SERVICE="${2:-all}"
        cd "$ASES_DIR/docker" && docker compose logs -f "$SERVICE"
        ;;

    restart)
        SERVICE="${2:-}"
        cd "$ASES_DIR/docker" && docker compose restart "$SERVICE"
        echo "Restarted $SERVICE"
        ;;

    update)
        echo "Updating ASES..."
        cd "$ASES_DIR/docker"
        docker compose pull
        docker compose up -d
        echo "Update complete"
        ;;

    backup)
        ~/bin/ases-backup
        ;;

    db-shell)
        cd "$ASES_DIR/docker" && docker compose exec postgres psql -U ases -d ases_production
        ;;

    clean-sandboxes)
        echo "Cleaning expired sandboxes..."
        docker ps -q --filter "name=ases-" | xargs -r docker stop
        docker system prune -f
        echo "Cleanup complete"
        ;;

    test-agent)
        echo "Testing agent service..."
        curl -s http://localhost:8000/health | jq . || echo "Agent not responding"
        echo ""
        echo "Testing dev task..."
        curl -s -X POST http://localhost:8000/dev-task \
            -H "Content-Type: application/json" \
            -d '{
                "action": "scaffold",
                "task": "Hello World API",
                "tech_stack": "Node.js + Express",
                "project_name": "test-api",
                "tenant_id": "default"
            }' | jq . || echo "Dev task failed"
        ;;

    costs)
        echo "=== Monthly Execution Costs ==="
        cd "$ASES_DIR/docker"
        docker compose exec -T postgres psql -U ases -d ases_production -c \
            "SELECT task_type, COUNT(*), SUM(cost_usd), AVG(compute_seconds) \
            FROM executions WHERE created_at > NOW() - INTERVAL '30 days' \
            GROUP BY task_type;"
        ;;

    *)
        echo "ASES Operations Script"
        echo ""
        echo "Usage: ./ases-ops.sh <command> [option]"
        echo ""
        echo "Commands:"
        echo "  status              Show service status"
        echo "  logs [service]      View logs (default: all)"
        echo "  restart [service]   Restart services"
        echo "  update              Pull latest images and restart"
        echo "  backup              Run manual backup"
        echo "  db-shell            Open PostgreSQL shell"
        echo "  clean-sandboxes     Remove old Docker sandboxes"
        echo "  test-agent          Test agent health and dev task"
        echo "  costs               Show execution costs"
        ;;
esac
