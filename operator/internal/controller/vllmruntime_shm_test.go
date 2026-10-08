package controller

import (
	"context"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	productionstackv1alpha1 "production-stack/api/v1alpha1"
)

func TestInvalidShmSizeDoesNotCreateResources(t *testing.T) {
	for _, size := range []string{"not-a-quantity", "0", "-1Gi"} {
		t.Run(size, func(t *testing.T) {
			ctx := context.Background()
			scheme := runtime.NewScheme()
			for _, add := range []func(*runtime.Scheme) error{
				productionstackv1alpha1.AddToScheme, corev1.AddToScheme, appsv1.AddToScheme,
			} {
				if err := add(scheme); err != nil {
					t.Fatal(err)
				}
			}
			runtime := &productionstackv1alpha1.VLLMRuntime{
				ObjectMeta: metav1.ObjectMeta{Name: "invalid-shm", Namespace: "default"},
			}
			runtime.Spec.DeploymentConfig.ShmSize = size
			client := fake.NewClientBuilder().WithScheme(scheme).WithObjects(runtime).Build()
			reconciler := &VLLMRuntimeReconciler{Client: client, Scheme: scheme}
			key := types.NamespacedName{Name: runtime.Name, Namespace: runtime.Namespace}

			_, err := reconciler.Reconcile(ctx, ctrl.Request{NamespacedName: key})
			if err == nil || !strings.Contains(err.Error(), "shmSize") {
				t.Fatalf("Reconcile() error = %v, want invalid shmSize error", err)
			}
			if err := client.Get(ctx, key, &corev1.Service{}); !apierrors.IsNotFound(err) {
				t.Errorf("Service lookup error = %v, want NotFound", err)
			}
			if err := client.Get(ctx, key, &appsv1.Deployment{}); !apierrors.IsNotFound(err) {
				t.Errorf("Deployment lookup error = %v, want NotFound", err)
			}

		})
	}
}

func TestValidShmSizeBoundsDeploymentVolume(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := productionstackv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	runtime := &productionstackv1alpha1.VLLMRuntime{
		ObjectMeta: metav1.ObjectMeta{Name: "valid-shm", Namespace: "default"},
	}
	runtime.Spec.DeploymentConfig.ShmSize = "1Gi"
	dep := (&VLLMRuntimeReconciler{Scheme: scheme}).deploymentForVLLMRuntime(runtime)
	for _, volume := range dep.Spec.Template.Spec.Volumes {
		if volume.Name != "dshm" {
			continue
		}
		if volume.EmptyDir == nil || volume.EmptyDir.Medium != corev1.StorageMediumMemory ||
			volume.EmptyDir.SizeLimit == nil || volume.EmptyDir.SizeLimit.Value() != 1073741824 {
			t.Fatalf("dshm volume = %#v, want memory emptyDir limited to 1Gi", volume)
		}
		return
	}
	t.Fatal("dshm volume missing from Deployment")
}

func TestEmptyShmSizeOmitsDeploymentVolume(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := productionstackv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	runtime := &productionstackv1alpha1.VLLMRuntime{
		ObjectMeta: metav1.ObjectMeta{Name: "default-shm", Namespace: "default"},
	}
	dep := (&VLLMRuntimeReconciler{Scheme: scheme}).deploymentForVLLMRuntime(runtime)
	for _, volume := range dep.Spec.Template.Spec.Volumes {
		if volume.Name == "dshm" {
			t.Fatalf("unexpected dshm volume: %#v", volume)
		}
	}
}
