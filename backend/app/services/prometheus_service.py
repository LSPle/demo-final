import requests
import logging
from typing import Dict, Optional, Any
from flask import current_app

logger = logging.getLogger(__name__)


class PrometheusService:
    """Prometheus service for querying metrics data"""
    
    def __init__(self):
        self.base_url = None
        self.timeout = 10

    def _get_base_url(self) -> str:
        """Get Prometheus base URL from config"""
        if not self.base_url:
            self.base_url = current_app.config.get('PROMETHEUS_BASE_URL', 'http://localhost:9090')
            # Remove '/classic/graph' suffix if present and use API endpoint
            if self.base_url.endswith('/classic/graph'):
                self.base_url = self.base_url.replace('/classic/graph', '')
        return self.base_url

    def _query_prometheus(self, query: str) -> Optional[Dict[str, Any]]:
        """Execute PromQL query against Prometheus API"""
        try:
            url = f"{self._get_base_url()}/api/v1/query"
            params = {'query': query}
            
            response = requests.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            
            data = response.json()
            if data.get('status') == 'success':
                return data.get('data', {})
            else:
                logger.error(f"Prometheus query failed: {data.get('error', 'Unknown error')}")
                return None
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to query Prometheus: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error querying Prometheus: {e}")
            return None

    # --- Helpers for common patterns ---
    def _query_value(self, query: str) -> Optional[float]:
        """Run a query and parse single scalar value from the first series."""
        result = self._query_prometheus(query)
        if result and result.get('result'):
            try:
                return float(result['result'][0]['value'][1])
            except (IndexError, ValueError, KeyError):
                return None
        return None

    def _first_nonempty(self, queries: list[str]) -> Optional[float]:
        """Try a list of queries in order and return first non-empty numeric result."""
        for q in queries:
            val = self._query_value(q)
            if val is not None:
                return val
        return None

    def get_cpu_usage(self, service_name: str) -> Optional[float]:
        """Get CPU usage percentage for a service"""
        # PromQL query for CPU usage - adjust based on your Prometheus setup
        query = f'100 - (avg(irate(node_cpu_seconds_total{{mode="idle",instance=~".*{service_name}.*"}}[5m])) * 100)'
        
        result = self._query_prometheus(query)
        if result and result.get('result'):
            try:
                value = float(result['result'][0]['value'][1])
                return round(value, 2)
            except (IndexError, ValueError, KeyError):
                logger.warning(f"Could not parse CPU usage for service {service_name}")
        
        return None
    
    def get_memory_usage(self, service_name: str) -> Optional[float]:
        """Get memory usage percentage for a service"""
        # PromQL query for memory usage - adjust based on your Prometheus setup
        query = f'(1 - (node_memory_MemAvailable_bytes{{instance=~".*{service_name}.*"}} / node_memory_MemTotal_bytes{{instance=~".*{service_name}.*"}})) * 100'
        
        result = self._query_prometheus(query)
        if result and result.get('result'):
            try:
                value = float(result['result'][0]['value'][1])
                return round(value, 2)
            except (IndexError, ValueError, KeyError):
                logger.warning(f"Could not parse memory usage for service {service_name}")
        
        return None
    
    def get_disk_usage(self, service_name: str) -> Optional[Dict[str, Any]]:
        """Get disk usage information for a service"""
        # PromQL queries for disk usage
        used_query = f'node_filesystem_size_bytes{{instance=~".*{service_name}.*",fstype!="tmpfs"}} - node_filesystem_free_bytes{{instance=~".*{service_name}.*",fstype!="tmpfs"}}'
        total_query = f'node_filesystem_size_bytes{{instance=~".*{service_name}.*",fstype!="tmpfs"}}'
        
        used_result = self._query_prometheus(used_query)
        total_result = self._query_prometheus(total_query)
        
        if used_result and total_result and used_result.get('result') and total_result.get('result'):
            try:
                used_bytes = float(used_result['result'][0]['value'][1])
                total_bytes = float(total_result['result'][0]['value'][1])
                
                used_gb = round(used_bytes / (1024**3), 1)
                total_gb = round(total_bytes / (1024**3), 1)
                usage_percent = round((used_bytes / total_bytes) * 100, 2)
                
                return {
                    'used_gb': used_gb,
                    'total_gb': total_gb,
                    'usage_percent': usage_percent,
                    'storage_display': f'{used_gb}GB / {total_gb}GB'
                }
            except (IndexError, ValueError, KeyError):
                logger.warning(f"Could not parse disk usage for service {service_name}")
        
        return None

    # NEW: MySQL QPS/TPS/P95 and Disk IO latency helpers
    def get_qps(self) -> Optional[float]:
        """Queries per second (QPS) based on mysql exporter metrics."""
        queries = [
            # Prefer Queries over Questions in most setups
            'sum(rate(mysql_global_status_queries[1m]))',
            'sum(rate(mysql_global_status_questions[1m]))',
            'sum(irate(mysql_global_status_queries[5m]))',
            'sum(irate(mysql_global_status_questions[5m]))',
        ]
        val = self._first_nonempty(queries)
        return round(val, 2) if isinstance(val, (float, int)) else None

    def get_tps(self) -> Optional[float]:
        """Transactions per second (TPS) using commit+rollback rates."""
        queries = [
            'sum(rate(mysql_global_status_com_commit[1m])) + sum(rate(mysql_global_status_com_rollback[1m]))',
            'sum(irate(mysql_global_status_com_commit[5m])) + sum(irate(mysql_global_status_com_rollback[5m]))',
        ]
        val = self._first_nonempty(queries)
        return round(val, 2) if isinstance(val, (float, int)) else None

    def get_p95_latency_ms(self) -> Optional[float]:
        """Approximate P95 query latency in milliseconds if histogram metrics are available."""
        candidates = [
            # query_response_time plugin via information_schema
            '1000 * histogram_quantile(0.95, sum(rate(mysql_info_schema_query_response_time_seconds_bucket[5m])) by (le))',
            # performance_schema events statements histogram (naming varies by exporter)
            '1000 * histogram_quantile(0.95, sum(rate(mysql_perf_schema_events_statements_seconds_bucket[5m])) by (le))',
        ]
        for q in candidates:
            val = self._query_value(q)
            if isinstance(val, (float, int)):
                # Sometimes histogram_quantile returns NaN when buckets empty
                try:
                    if not (val != val):  # filter NaN
                        return round(val, 2)
                except Exception:
                    return round(val, 2)
        return None

    def get_disk_io_latency_ms(self, device_regex: str = ".*") -> Optional[float]:
        """Average disk I/O latency (ms/op), weighted across devices.
        Requires node_exporter metrics. Uses read/write time divided by ops as approximation.
        """
        # Prefer standard metric names; try fallbacks for compatibility
        read_time = f'sum(rate(node_disk_read_time_seconds_total[5m]))'
        write_time = f'sum(rate(node_disk_write_time_seconds_total[5m]))'
        read_ops = f'sum(rate(node_disk_reads_completed_total[5m]))'
        write_ops = f'sum(rate(node_disk_writes_completed_total[5m]))'
        
        # Apply device filter if provided
        if device_regex and device_regex != ".*":
            read_time = f'sum(rate(node_disk_read_time_seconds_total{{device=~"{device_regex}"}}[5m]))'
            write_time = f'sum(rate(node_disk_write_time_seconds_total{{device=~"{device_regex}"}}[5m]))'
            read_ops = f'sum(rate(node_disk_reads_completed_total{{device=~"{device_regex}"}}[5m]))'
            write_ops = f'sum(rate(node_disk_writes_completed_total{{device=~"{device_regex}"}}[5m]))'

        # Compute combined latency = 1000 * (sum(read_time)+sum(write_time)) / (sum(read_ops)+sum(write_ops))
        combined_query = f'1000 * (({read_time}) + ({write_time})) / (({read_ops}) + ({write_ops}))'
        val = self._query_value(combined_query)
        if isinstance(val, (float, int)):
            try:
                if not (val != val):  # NaN guard
                    return round(val, 2)
            except Exception:
                return round(val, 2)

        # Fallback: try io_time/ops if read/write metrics missing
        io_time = 'sum(rate(node_disk_io_time_seconds_total[5m]))'
        ops_any = f'(({read_ops}) + ({write_ops}))'
        fallback = f'1000 * ({io_time}) / ({ops_any})'
        val2 = self._query_value(fallback)
        if isinstance(val2, (float, int)):
            try:
                if not (val2 != val2):
                    return round(val2, 2)
            except Exception:
                return round(val2, 2)
        return None
    
    def get_all_metrics(self, service_name: str) -> Dict[str, Any]:
        """Get all metrics (CPU, memory, disk) for a service"""
        metrics = {
            'service': service_name,
            'cpu_usage': self.get_cpu_usage(service_name),
            'memory_usage': self.get_memory_usage(service_name),
            'disk_usage': self.get_disk_usage(service_name),
            'timestamp': None
        }
        
        # Add timestamp if we have at least one metric
        if any(v is not None for k, v in metrics.items() if k not in ['service', 'timestamp']):
            import time
            metrics['timestamp'] = int(time.time())
        
        return metrics
    
    def health_check(self) -> bool:
        """Check if Prometheus is accessible"""
        try:
            url = f"{self._get_base_url()}/api/v1/status/config"
            response = requests.get(url, timeout=5)
            return response.status_code == 200
        except Exception:
            return False


# Global instance
prometheus_service = PrometheusService()