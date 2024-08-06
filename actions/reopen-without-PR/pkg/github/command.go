package github

import (
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/google/uuid"
)

// Refer to the link for details: https://docs.github.com/en/actions/using-workflows/workflow-commands-for-github-actions

const (
	CmdString = "::"
)

func Run(command string, property Property, message string) error {
	cmd := CmdString + command
	if property != nil {
		cmd += " "
		cmd += property.ToString()
	}
	cmd += CmdString + message + "\n"

	// run command
	_, err := os.Stdout.WriteString(cmd)
	return err
}

func writeCommandFile(commandFileName string, data string) error {
	f, err := os.OpenFile(commandFileName, os.O_WRONLY|os.O_APPEND, os.ModeAppend)
	if err != nil {
		return errors.Join(errors.New("failed to open output file"), err)
	}
	defer f.Close()
	_, err = f.WriteString(fmt.Sprintf("%s\n", data))
	return err
}

// output like
// test<<ghadelimiter_6a0c499a-6a40-4dae-aba3-0926e89a4df9
//
// test
// ghadelimiter_6a0c499a-6a40-4dae-aba3-0926e89a4df9
func prepareKeyValueMessage(key, value string) string {
	delimiter := genDelimiter()

	// These should realistically never happen, but just in case someone finds a
	// way to exploit uuid generation let's not allow keys or values that contain
	// the delimiter.
	if strings.Contains(key, delimiter) {
		panic("Unexpected input: key should not contain the delimiter " + delimiter)
	}
	if strings.Contains(value, delimiter) {
		panic("Unexpected input: value should not contain the delimiter " + delimiter)
	}

	return fmt.Sprintf("%s<<%s\n%s\n%s", key, delimiter, value, delimiter)
}

func genDelimiter() string {
	id, _ := uuid.NewV7()
	return fmt.Sprintf("ghadelimiter_%s", id)
}
