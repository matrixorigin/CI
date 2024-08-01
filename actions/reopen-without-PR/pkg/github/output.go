package github

import (
	"fmt"
	"os"
)

type Output struct {
	Description string `yaml:"description"`
}

type Outputs map[string]Output

// SetOutput sets a step's output parameter.
func SetOutput(key, value string) error {
	outputs := os.Getenv("GITHUB_OUTPUT")
	if outputs == "" {
		return deprecatedSetOutput(key, value)
	}
	return writeCommandFile(outputs, prepareKeyValueMessage(key, value))
}

// deprecated:
type deprecatedOutputCommand string

func (d *deprecatedOutputCommand) Set(key, value string) error {
	return nil
}

func (d *deprecatedOutputCommand) Get(key string) string {
	return string(*d)
}

func (d *deprecatedOutputCommand) ToString() string {
	return fmt.Sprintf("name=%s", *d)
}

// deprecated:
func deprecatedSetOutput(key, value string) error {
	dKey := deprecatedOutputCommand(key)
	return Run("set-output", &dKey, value)
}
