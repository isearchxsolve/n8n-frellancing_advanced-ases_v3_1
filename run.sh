#!/bin/bash
# ASES Multi-Container Run Script

set -e

CMD="${1:-help}"
COMPOSE_FILE="docker-compose.yml"

case "$CMD" in
    up|start)
        echo "Starting ASES production stack..."
        docker compose -f "$COMPOSE_FILE" up -d
        echo ""
        echo "Waiting for services..."
        sleep 15
        echo ""
        echo "=== Service Status ==="
        docker compose -f "$COMPOSE_FILE" ps
        echo ""
        echo "=== Health Checks ==="
        docker compose -f "$COMPOSE_FILE" exec agent python3 -c \
            "import urllib.request; print('Agent:', urllib.request.urlopen('http://localhost:8000/health').status)" 2>/dev/null || echo "Agent: checking..."
        docker compose -f "$COMPOSE_FILE" exec n8n wget -qO- http://localhost:5678/healthz 2>/dev/null || echo "n8n: checking..."
        echo ""
        echo "URLs:"
        echo "  n8n UI:       https://${DOMAIN:-localhost}"
        echo "  Agent API:    https://${DOMAIN:-localhost}/api/"
        echo "  API Docs:     https://${DOMAIN:-localhost}/api/docs"
        ;;

    down|stop)
        echo "Stopping ASES..."
        docker compose -f "$COMPOSE_FILE" down
        echo "Stopped"
        ;;

    restart)
        SERVICE="${2:-}"
        if [[ -n "$SERVICE" ]]; then
            echo "Restarting $SERVICE..."
            docker compose -f "$COMPOSE_FILE" restart "$SERVICE"
        else
            echo "Restarting all services..."
            docker compose -f "$COMPOSE_FILE" restart
        fi
        ;;

    logs)
        SERVICE="${2:-}"
        if [[ -n "$SERVICE" ]]; then
            docker compose -f "$COMPOSE_FILE" logs -f "$SERVICE"
        else
            docker compose -f "$COMPOSE_FILE" logs -f
        fi
        ;;

    ps|status)
        docker compose -f "$COMPOSE_FILE" ps
        ;;

    build)
        echo "Building images..."
        docker compose -f "$COMPOSE_FILE" build --no-cache
        ;;

    pull)
        echo "Pulling latest images..."
        docker compose -f "$COMPOSE_FILE" pull
        ;;

    update)
        echo "Updating ASES..."
        docker compose -f "$COMPOSE_FILE" pull
        docker compose -f "$COMPOSE_FILE" up -d
        ;;

    shell)
        SERVICE="${2:-agent}"
        docker compose -f "$COMPOSE_FILE" exec "$SERVICE" bash
        ;;

    db)
        docker compose -f "$COMPOSE_FILE" exec postgres psql -U ases -d ases_production
        ;;

    backup)
        BACKUP_DIR="/backup/ases"
        DATE=$(date +%Y%m%d_%H%M%S)
        mkdir -p "$BACKUP_DIR"

        echo "Backing up database..."
        docker compose -f "$COMPOSE_FILE" exec -T postgres pg_dump -U ases ases_production > "$BACKUP_DIR/db_$DATE.sql"

        echo "Backing up n8n..."
        docker compose -f "$COMPOSE_FILE" cp n8n:/home/node/.n8n "$BACKUP_DIR/n8n_$DATE"

        echo "Backup complete: $BACKUP_DIR"
        ;;

    clean)
        echo "Cleaning up..."
        docker compose -f "$COMPOSE_FILE" down -v
        docker system prune -f
        echo "Cleaned"
        ;;

    test)
        echo "Testing agent service..."
        curl -s http://localhost:8000/health | jq . 2>/dev/null || echo "Agent not responding"
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
            }' | jq . 2>/dev/null || echo "Dev task failed"
        ;;

    costs)
        echo "=== Execution Costs (Last 30 Days) ==="
        docker compose -f "$COMPOSE_FILE" exec -T postgres psql -U ases -d ases_production -c \
            "SELECT task_type, COUNT(*) as runs, SUM(cost_usd) as total_cost, AVG(compute_seconds) as avg_duration \
            FROM executions WHERE created_at > NOW() - INTERVAL '30 days' \
            GROUP BY task_type ORDER BY total_cost DESC;"
        ;;

    *)
        echo "ASES Multi-Container Operations"
        echo ""
        echo "Usage: ./run.sh <command> [option]"
        echo ""
        echo "Commands:"
        echo "  up              Start all services"
        echo "  down            Stop all services"
        echo "  restart [svc]   Restart service(s)"
        echo "  logs [svc]      View logs"
        echo "  ps              Show service status"
        echo "  build           Build images from scratch"
        echo "  pull            Pull latest images"
        echo "  update          Pull and restart"
        echo "  shell [svc]     Open shell in container (default: agent)"
        echo "  db              Open PostgreSQL shell"
        echo "  backup          Manual backup"
        echo "  clean           Stop and remove volumes"
        echo "  test            Test agent health and dev task"
        echo "  costs           Show execution costs"
        echo ""
        echo "Examples:"
        echo "  ./run.sh up"
        echo "  ./run.sh logs agent"
        echo "  ./run.sh shell n8n"
        echo "  ./run.sh restart agent"
        ;;
esac
