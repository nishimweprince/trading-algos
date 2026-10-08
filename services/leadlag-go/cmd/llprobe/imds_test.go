package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestIMDSReadsEC2AndAzureAndIgnoresErrorDocuments(t *testing.T) {
	client := &http.Client{Timeout: time.Second}

	ec2 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPut && r.URL.Path == "/latest/api/token":
			_, _ = w.Write([]byte("tok"))
		case r.Header.Get("X-aws-ec2-metadata-token") != "tok":
			w.WriteHeader(http.StatusUnauthorized)
		case r.URL.Path == "/latest/meta-data/placement/availability-zone-id":
			_, _ = w.Write([]byte("apne1-az1"))
		case r.URL.Path == "/latest/meta-data/placement/availability-zone":
			_, _ = w.Write([]byte("ap-northeast-1a"))
		case r.URL.Path == "/latest/meta-data/instance-type":
			_, _ = w.Write([]byte("c7i.xlarge"))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer ec2.Close()
	if id, az, it := imdsAt(ec2.URL, client); id != "apne1-az1" || az != "ap-northeast-1a" || it != "c7i.xlarge" {
		t.Fatalf("ec2: got %q %q %q", id, az, it)
	}

	// Azure answers EC2's paths with 411/404 XML documents and serves its own API.
	azure := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/metadata/instance/compute" && r.Header.Get("Metadata") == "true" {
			_, _ = w.Write([]byte(`{"location":"japanwest","zone":"","vmSize":"Standard_D4ads_v6"}`))
			return
		}
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte("<Error><Code>ResourceNotFound</Code></Error>"))
	}))
	defer azure.Close()
	if id, az, it := imdsAt(azure.URL, client); id != "azure:japanwest" || az != "japanwest" || it != "Standard_D4ads_v6" {
		t.Fatalf("azure: got %q %q %q", id, az, it)
	}

	nothing := httptest.NewServer(http.NotFoundHandler())
	defer nothing.Close()
	if id, az, it := imdsAt(nothing.URL, client); id != "" || az != "" || it != "" {
		t.Fatalf("no metadata: got %q %q %q", id, az, it)
	}
}
