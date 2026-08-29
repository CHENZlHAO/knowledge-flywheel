//go:build windows

package main

import (
	"fmt"
	"os"
	"time"

	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"
)

const serviceName = "KnowledgeEdgeAgent"
const serviceDescription = "Knowledge Flywheel edge agent: file hashing, heartbeat, and fixed-replica sync"

func handleServiceCommand(cfg Config) error {
	switch cfg.Service {
	case "install":
		return installService(cfg)
	case "uninstall":
		return uninstallService()
	case "run":
		return runService(cfg)
	default:
		return fmt.Errorf("unknown service command %q", cfg.Service)
	}
}

func installService(cfg Config) error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	m, err := mgr.Connect()
	if err != nil {
		return err
	}
	defer m.Disconnect()
	existing, err := m.OpenService(serviceName)
	if err == nil {
		existing.Close()
		return fmt.Errorf("service %s already exists; uninstall it first", serviceName)
	}
	args := serviceArgs(cfg)
	service, err := m.CreateService(serviceName, exe, mgr.Config{
		DisplayName: serviceName,
		Description: serviceDescription,
		StartType:   mgr.StartAutomatic,
	}, args...)
	if err != nil {
		return err
	}
	defer service.Close()
	return nil
}

func uninstallService() error {
	m, err := mgr.Connect()
	if err != nil {
		return err
	}
	defer m.Disconnect()
	service, err := m.OpenService(serviceName)
	if err != nil {
		return err
	}
	defer service.Close()
	return service.Delete()
}

func serviceArgs(cfg Config) []string {
	args := []string{"-service", "run"}
	if cfg.NodeID != "" {
		args = append(args, "-node-id", cfg.NodeID)
	}
	if cfg.CenterURL != "" {
		args = append(args, "-center-url", cfg.CenterURL)
	}
	if cfg.WatchDir != "" {
		args = append(args, "-watch-dir", cfg.WatchDir)
	}
	if cfg.NodeAPIKey != "" {
		args = append(args, "-node-api-key", cfg.NodeAPIKey)
	}
	if cfg.IsReplica {
		args = append(args, "-is-replica")
	}
	return args
}

func runService(cfg Config) error {
	return svc.Run(serviceName, &edgeAgentService{cfg: cfg})
}

type edgeAgentService struct {
	cfg Config
}

func (s *edgeAgentService) Execute(_ []string, requests <-chan svc.ChangeRequest, status chan<- svc.Status) (bool, uint32) {
	const accepted = svc.AcceptStop | svc.AcceptShutdown
	status <- svc.Status{State: svc.StartPending}
	stop := make(chan struct{})
	errCh := make(chan error, 1)
	go func() { errCh <- runAgentLoop(s.cfg, stop) }()
	status <- svc.Status{State: svc.Running, Accepts: accepted}
	for {
		select {
		case change := <-requests:
			switch change.Cmd {
			case svc.Interrogate:
				status <- change.CurrentStatus
			case svc.Stop, svc.Shutdown:
				close(stop)
				status <- svc.Status{State: svc.StopPending}
				select {
				case <-errCh:
				case <-time.After(10 * time.Second):
				}
				status <- svc.Status{State: svc.Stopped}
				return false, 0
			default:
				status <- change.CurrentStatus
			}
		case <-errCh:
			status <- svc.Status{State: svc.Stopped}
			return false, 1
		}
	}
}
