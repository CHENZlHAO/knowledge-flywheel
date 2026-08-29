//go:build !windows

package main

import "fmt"

// handleServiceCommand exists only so the -service flag fails clearly on
// non-Windows platforms; the actual Windows SCM integration lives in
// service_windows.go.
func handleServiceCommand(cfg Config) error {
	return fmt.Errorf("Windows service commands are only supported on Windows (got %q)", cfg.Service)
}
