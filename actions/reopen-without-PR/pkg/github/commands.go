package github

import (
	"fmt"
	"os"
)

// Info prints an info message to the log.
func Info(message string) error {
	_, err := os.Stdout.WriteString(message + "\n")
	return err
}

// Debug prints a debug message to the log.
func Debug(message string) error {
	return Run("debug", nil, message)
}

// AddMask mask a value prevents a string or variable from being printed in the log
func AddMask(content string) error {
	return Run("add-mask", nil, content)
}

// Stop stops processing any workflow commands
func Stop(token string) error {
	return Run("stop-commands", nil, token)
}

// Start processing workflow commands, pass the same token that you used to stop workflow commands
func Start(token string) error {
	return Run(token, nil, "")
}

// Summary You can set some custom Markdown for each job so that it will be displayed on the summary page of a workflow run.
// You can use job summaries to display and group unique content, such as test result summaries,
// so that someone viewing the result of a workflow run doesn't need to go into the logs to see important information related to the run,
// such as failures.
func Summary(message string) error {
	summaryFile := os.Getenv("GITHUB_STEP_SUMMARY")
	return writeCommandFile(summaryFile, message)
}

// AddPath prepends a directory to the system PATH variable and automatically makes it available to all subsequent actions in the current job
// the currently running action cannot access the updated path variable.
func AddPath(path string) error {
	pathFile := os.Getenv("GITHUB_PATH")
	err := writeCommandFile(pathFile, path)
	if err != nil {
		return err
	}
	return os.Setenv("PATH", fmt.Sprintf("%s:%s", os.Getenv("PATH"), path))
}
