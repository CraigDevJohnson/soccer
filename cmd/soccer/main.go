// The Schedule Downloader backend. On Lambda it serves through the Function
// URL adapter (payload format 2.0, the same shape API Gateway's HTTP API
// sends); anywhere else it listens on a local port.
package main

import (
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/aws/aws-lambda-go/lambda"
	"github.com/awslabs/aws-lambda-go-api-proxy/httpadapter"

	"github.com/CraigDevJohnson/soccer/internal/api"
	"github.com/CraigDevJohnson/soccer/internal/league"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))

	client := league.NewClient(os.Getenv("LEAGUE_BASE_URL"))
	if id := os.Getenv("LEAGUE_FACILITY_ID"); id != "" {
		client.FacilityID = id
	}
	h := &api.Handler{
		Teams:       client,
		Directory:   league.NewScanDirectory(client, log),
		LinkBaseURL: os.Getenv("LINK_BASE_URL"),
		Log:         log,
	}
	handler := h.Router()

	if os.Getenv("AWS_LAMBDA_RUNTIME_API") != "" {
		lambda.Start(httpadapter.NewV2(handler).ProxyWithContext)
		return
	}
	addr := os.Getenv("SOCCER_ADDR")
	if addr == "" {
		addr = "127.0.0.1:8081"
	}
	srv := &http.Server{Addr: addr, Handler: handler, ReadHeaderTimeout: 5 * time.Second}
	log.Info("listening", "addr", "http://"+addr)
	if err := srv.ListenAndServe(); err != nil {
		log.Error("serve", "err", err)
		os.Exit(1)
	}
}
