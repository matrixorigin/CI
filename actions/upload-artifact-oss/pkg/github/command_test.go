package github

import (
	"fmt"
	"testing"
)

func Test_GenDelimiter(t *testing.T) {
	fmt.Printf(genDelimiter())
}

func Test_prepareKeyValueMessage(t *testing.T) {
	fmt.Printf(prepareKeyValueMessage("test", "test"))
}
