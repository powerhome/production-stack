package controller

import (
	"context"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"

	productionstackv1alpha1 "production-stack/api/v1alpha1"
)

func testRuntimeDeployment(t *testing.T) (*VLLMRuntimeReconciler, *productionstackv1alpha1.VLLMRuntime) {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := productionstackv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	vr := &productionstackv1alpha1.VLLMRuntime{
		ObjectMeta: metav1.ObjectMeta{Name: "test-runtime", Namespace: "default"},
	}
	vr.Spec.DeploymentConfig.NodeSelectorTerms = []corev1.NodeSelectorTerm{{}}
	return &VLLMRuntimeReconciler{Scheme: scheme}, vr
}

func TestRuntimeClassOnlySetWhenConfigured(t *testing.T) {
	r, vr := testRuntimeDeployment(t)
	if got := r.deploymentForVLLMRuntime(vr).Spec.Template.Spec.RuntimeClassName; got != nil {
		t.Fatalf("unset runtime class rendered as %q, want nil", *got)
	}
	vr.Spec.DeploymentConfig.RuntimeClass = "nvidia"
	got := r.deploymentForVLLMRuntime(vr).Spec.Template.Spec.RuntimeClassName
	if got == nil || *got != "nvidia" {
		t.Fatalf("configured runtime class rendered as %v, want nvidia", got)
	}
}

func TestDeploymentNeedsUpdateForRemovedPodAnnotation(t *testing.T) {
	r, vr := testRuntimeDeployment(t)
	vr.Spec.DeploymentConfig.PodAnnotations = map[string]string{"keep": "value"}
	dep := r.deploymentForVLLMRuntime(vr)
	dep.Spec.Template.Annotations = map[string]string{"keep": "value", "removed": "stale"}
	if !r.deploymentNeedsUpdate(context.Background(), dep, vr) {
		t.Fatal("stale annotation did not trigger Deployment update")
	}
	dep.Spec.Template.Annotations = map[string]string{"keep": "value"}
	if r.deploymentNeedsUpdate(context.Background(), dep, vr) {
		t.Fatal("identical annotations triggered Deployment update")
	}
	vr.Spec.DeploymentConfig.PodAnnotations = nil
	dep.Spec.Template.Annotations = map[string]string{}
	if r.deploymentNeedsUpdate(context.Background(), dep, vr) {
		t.Fatal("empty and nil annotation maps triggered Deployment update")
	}
}
