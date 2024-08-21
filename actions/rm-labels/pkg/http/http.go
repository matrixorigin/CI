package http

import (
	"fmt"
	"io"
	"net/http"
	"time"
)

// 创建HTTP客户端并设置超时
var client = &http.Client{Timeout: 10 * time.Second}

func Request(method, url, token string, data io.Reader, contentType string) (body []byte, err error) {
	req, err := http.NewRequest(method, url, data)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", token))
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}

	fmt.Sprintf("==========\nurl: %s\nmethod:%s\n==========")

	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("client.Do(req) failed: %v", err)
	}

	if !(resp.StatusCode >= 200 && resp.StatusCode < 400) {
		return nil, fmt.Errorf("resp.StatusCode is not correct: %v", resp.StatusCode)
	}

	body, err = io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("io.ReadAll(resp.Body) failed: %v", err)
	}

	return body, err
}
