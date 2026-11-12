// Decode Kubecost federated hourly asset files (bingen) into one JSON line per Node asset.
//
// Read-only: takes local file paths (downloaded from
// s3://cloudpipe-finops/federated/cloudpipe/etl/bingen/assets/1h/) and writes JSONL to stdout.
// For #1060 phase B: which EC2 instance held which hostname, when, of what type and capacity.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"github.com/opencost/opencost/core/pkg/opencost"
)

type row struct {
	File          string            `json:"file"`
	Name          string            `json:"name"`
	ProviderID    string            `json:"provider_id"`
	Cluster       string            `json:"cluster"`
	NodeType      string            `json:"node_type"`
	Start         string            `json:"start"`
	End           string            `json:"end"`
	Minutes       float64           `json:"minutes"`
	Preemptible   float64           `json:"preemptible"`
	CPUCoreHours  float64           `json:"cpu_core_hours"`
	RAMByteHours  float64           `json:"ram_byte_hours"`
	GPUHours      float64           `json:"gpu_hours"`
	GPUCount      float64           `json:"gpu_count"`
	CPUCost       float64           `json:"cpu_cost"`
	RAMCost       float64           `json:"ram_cost"`
	GPUCost       float64           `json:"gpu_cost"`
	Discount      float64           `json:"discount"`
	Adjustment    float64           `json:"adjustment"`
	InstanceLabel string            `json:"instance_type_label"`
	CapacityLabel string            `json:"capacity_type_label"`
	ZoneLabel     string            `json:"zone_label"`
	Labels        map[string]string `json:"labels,omitempty"`
}

func main() {
	enc := json.NewEncoder(os.Stdout)
	withLabels := os.Getenv("KC_LABELS") == "1"
	failed := 0
	var paths []string
	for _, arg := range os.Args[1:] {
		if st, err := os.Stat(arg); err == nil && st.IsDir() {
			entries, _ := os.ReadDir(arg)
			for _, e := range entries {
				if !e.IsDir() {
					paths = append(paths, filepath.Join(arg, e.Name()))
				}
			}
		} else {
			paths = append(paths, arg)
		}
	}
	for _, path := range paths {
		data, err := os.ReadFile(path)
		if err != nil {
			fmt.Fprintf(os.Stderr, "%s: %v\n", path, err)
			failed++
			continue
		}
		as := &opencost.AssetSet{}
		if err := as.UnmarshalBinary(data); err != nil {
			fmt.Fprintf(os.Stderr, "%s: decode: %v\n", path, err)
			failed++
			continue
		}
		for _, a := range as.Assets {
			n, ok := a.(*opencost.Node)
			if !ok {
				continue
			}
			r := row{
				File: filepath.Base(path), NodeType: n.NodeType,
				Start: n.Start.UTC().Format("2006-01-02T15:04:05Z"), End: n.End.UTC().Format("2006-01-02T15:04:05Z"),
				Minutes: n.End.Sub(n.Start).Minutes(), Preemptible: n.Preemptible,
				CPUCoreHours: n.CPUCoreHours, RAMByteHours: n.RAMByteHours, GPUHours: n.GPUHours, GPUCount: n.GPUCount,
				CPUCost: n.CPUCost, RAMCost: n.RAMCost, GPUCost: n.GPUCost, Discount: n.Discount, Adjustment: n.Adjustment,
			}
			if p := n.Properties; p != nil {
				r.Name, r.ProviderID, r.Cluster = p.Name, p.ProviderID, p.Cluster
			}
			l := map[string]string(n.Labels)
			r.InstanceLabel = first(l, "node_kubernetes_io_instance_type", "beta_kubernetes_io_instance_type")
			r.CapacityLabel = first(l, "karpenter_sh_capacity_type", "eks_amazonaws_com_capacityType")
			r.ZoneLabel = first(l, "topology_kubernetes_io_zone", "failure_domain_beta_kubernetes_io_zone")
			if withLabels {
				r.Labels = l
			}
			if err := enc.Encode(r); err != nil {
				fmt.Fprintln(os.Stderr, err)
				os.Exit(1)
			}
		}
	}
	if failed > 0 {
		fmt.Fprintf(os.Stderr, "%d files failed\n", failed)
		os.Exit(2)
	}
}

func first(m map[string]string, keys ...string) string {
	for _, k := range keys {
		if v, ok := m[k]; ok {
			return v
		}
	}
	return ""
}
