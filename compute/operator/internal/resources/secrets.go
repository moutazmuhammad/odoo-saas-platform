package resources

import (
	"crypto/rand"
	"fmt"
	"math/big"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	saasv1alpha1 "github.com/freightright/odoo-saas-platform/operator/api/v1alpha1"
)

const passwordAlphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

// GeneratePassword returns a cryptographically random password of length n.
// Used only the first time a credentials Secret is created; the controller
// must always prefer an existing Secret's value over calling this again,
// or every reconcile would rotate customer database passwords.
func GeneratePassword(n int) (string, error) {
	b := make([]byte, n)
	max := big.NewInt(int64(len(passwordAlphabet)))
	for i := range b {
		idx, err := rand.Int(rand.Reader, max)
		if err != nil {
			return "", fmt.Errorf("generating random password: %w", err)
		}
		b[i] = passwordAlphabet[idx.Int64()]
	}
	return string(b), nil
}

// AdminSecretData is the desired content of the Odoo admin/master-password
// Secret.
type AdminSecretData struct {
	MasterPassword string
}

// AdminSecret builds the Secret holding Odoo's master password (used for
// database management operations via /web/database/manager). The caller is
// responsible for sourcing Data.MasterPassword from an existing Secret when
// one already exists, so reconciliation never rotates it silently.
func AdminSecret(instance *saasv1alpha1.OdooInstance, data AdminSecretData) *corev1.Secret {
	return &corev1.Secret{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "Secret"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      AdminSecretName(instance),
			Namespace: TenantNamespace(instance),
			Labels:    CommonLabels(instance),
		},
		Type: corev1.SecretTypeOpaque,
		StringData: map[string]string{
			"master-password": data.MasterPassword,
		},
	}
}

// DatabaseSecretData is the desired content of the database credentials
// Secret for a Managed or CloudNativePG-mode instance. External mode
// instances bring their own Secret and this builder is not used for them.
type DatabaseSecretData struct {
	Host     string
	Port     string
	DBName   string
	Username string
	Password string
}

// DatabaseSecret builds the Secret holding PostgreSQL connection details.
// As with AdminSecret, Password must be sourced from the existing Secret
// when present.
func DatabaseSecret(instance *saasv1alpha1.OdooInstance, data DatabaseSecretData) *corev1.Secret {
	return &corev1.Secret{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "Secret"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      DatabaseSecretName(instance),
			Namespace: TenantNamespace(instance),
			Labels:    CommonLabels(instance),
		},
		Type: corev1.SecretTypeOpaque,
		StringData: map[string]string{
			"host":     data.Host,
			"port":     data.Port,
			"dbname":   data.DBName,
			"username": data.Username,
			"password": data.Password,
		},
	}
}
