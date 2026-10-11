# Autoscale tester

A small toolkit to practise and test Kubernetes **Horizontal Pod Autoscaling (HPA)**.

- `workload/` is the app that gets scaled. Its `/burn?ms=200` endpoint uses CPU on demand.
- `tester/` is a dashboard plus load generator. It sends load to the workload **from inside the cluster**, through the Service, so traffic is spread across every pod. It also reads the HPA, pods and CPU from the Kubernetes API and shows them live.
- `k8s/` holds the manifests: workload, HPA, and the tester with read-only RBAC.

## Quick start (minikube, for example on your EC2 instance)

```bash
cd autoscale-tester
./deploy.sh
kubectl port-forward --address 0.0.0.0 svc/autoscale-tester 8080:80
```

Open `http://<EC2-PUBLIC-IP>:8080` (locally: `http://localhost:8080`). On EC2 the security group must allow port 8080 from your IP.

`deploy.sh` enables metrics-server, builds both images inside minikube, applies the manifests and restarts the pods.

The workload uses the same names as the earlier practice setup (`hpa-demo`), so `deploy.sh` replaces that Deployment, Service and HPA. If you want a clean start first:

```bash
kubectl delete deployment,service,hpa hpa-demo --ignore-not-found
```

## Using the dashboard

1. Click **Run full autoscale test**. It runs: warm up (5 users, 1 min), ramp up (15 users, 1.5 min), peak (40 users, 2 min), cool down (no load, 3 min).
2. Watch the cards and charts:
   - **Replicas** goes up a minute or so after CPU passes the target, and comes back down during cool down.
   - **CPU %** is the average across pods, measured against each pod's CPU *request* (100m). The dashed line is the HPA target (50%).
   - **Pods** shows status, CPU and each pod's share of the recent traffic. New pods start at 0%, then take a fair share once Ready.
   - **Events** logs every replica and load change with the CPU at that moment.
3. Or drive it by hand: set parallel users and CPU per request, pick a duration, press **Start / update load**. The presets are Light, Medium, Heavy and Spike.

## Experiments

Edit `k8s/workload.yaml`, then `kubectl apply -f k8s/workload.yaml`:

- Change `averageUtilization` to 30 or 80 and repeat the test.
- Change `maxReplicas`, or `requests.cpu` (the percentage is relative to the request).
- Add scale-up limits under `behavior`, for example `scaleUp: {policies: [{type: Pods, value: 2, periodSeconds: 30}]}`.
- Raise `stabilizationWindowSeconds` to 300 (the Kubernetes default) and see how much slower scale-down gets.
- Remove `resources.requests` and the HPA reports unknown CPU.

To change the automatic test, edit `STAGES` in `tester/app.py` and rebuild.

## Troubleshooting

| Dashboard shows | Cause and fix |
|---|---|
| "Cannot read cluster data ... 403" | RBAC missing. Re-apply `k8s/tester.yaml`. |
| "... 404 ... horizontalpodautoscalers" | HPA name differs. Set `HPA_NAME` in `k8s/tester.yaml`. |
| CPU shows `n/a (metrics not ready)` | `minikube addons enable metrics-server`, wait about 1 minute, check `kubectl top pods`. |
| Errors count keeps rising | Workload pods are not Ready or the Service name differs. Check `kubectl get pods,svc`. Set `TARGET_URL` if needed. |
| Replicas never rise | Load is too light. Use Heavy, and make sure the pods have CPU requests. |
| Page does not load | The port-forward must stay running, and the security group must allow the port. |

## Settings (environment variables on the tester)

`TARGET_URL` (default `http://hpa-demo`), `HPA_NAME` (`hpa-demo`), `POD_LABEL` (`app=hpa-demo`), `NAMESPACE` (the pod's own), `MAX_USERS` (`150`).

## What this does not cover

This scales **pods** (HPA). Scaling **nodes** (Cluster Autoscaler or Karpenter on EKS) needs a real multi-node cluster. Minikube has a single node, so you cannot practise that part here.

## Not for production

The dashboard has no login. Only expose it to your own IP.
