package github

import (
	"fmt"
	"os"
)

func GetState(key string) string {
	return GetInput(fmt.Sprintf("STATE_%s", key))
}

// SetState you can create environment variables for sharing with your workflow's pre: or post: actions by writing to the file located at GITHUB_STATE
func SetState(key, value string) error {
	state := os.Getenv("GITHUB_STATE")
	if state == "" {
		return deprecatedSetState(key, value)
	}
	return writeCommandFile(state, prepareKeyValueMessage(key, value))
}

// deprecated:
func deprecatedSetState(key, value string) error {
	dKey := deprecatedOutputCommand(key)
	return Run("set-state", &dKey, value)
}
