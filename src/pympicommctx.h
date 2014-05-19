/* Author:  Lisandro Dalcin   */
/* Contact: dalcinl@gmail.com */

#include <stdlib.h>
#ifndef PyMPI_MALLOC
#define PyMPI_MALLOC malloc
#endif
#ifndef PyMPI_FREE
#define PyMPI_FREE free
#endif

#ifndef MPIAPI
#define MPIAPI
#endif

#undef  CHKERR
#define CHKERR(ierr) do { if (ierr != MPI_SUCCESS) return ierr; } while(0)

typedef struct {
  MPI_Comm dupcomm;
  int      tag,tag_ub;
  MPI_Comm localcomm;
  int      low_group;
} PyMPI_Commctx;

static int PyMPI_Commctx_new(PyMPI_Commctx **_commctx)
{
  static int tag_ub = -1;
  PyMPI_Commctx *commctx;
  if (tag_ub < 0) {
    int ierr, *attrval = NULL, flag = 0;
    ierr = MPI_Comm_get_attr(MPI_COMM_WORLD, MPI_TAG_UB, &attrval, &flag); CHKERR(ierr);
    tag_ub = (flag && attrval) ? *attrval : 32767;
  }
  commctx = (PyMPI_Commctx *)PyMPI_MALLOC(sizeof(PyMPI_Commctx));
  if (commctx) {
    commctx->dupcomm = MPI_COMM_NULL;
    commctx->tag_ub = tag_ub;
    commctx->tag = 0;
    commctx->localcomm = MPI_COMM_NULL;
    commctx->low_group = -1;
  }
  *_commctx = commctx;
  return MPI_SUCCESS;
}

static int MPIAPI PyMPI_Commctx_free_fn(MPI_Comm comm, int k, void *v, void *xs)
{
  if (!v && comm == MPI_COMM_SELF) {
    (void)MPI_Comm_free_keyval(&k);
    (void)MPI_Comm_free_keyval((int *)xs);
  } else {
    PyMPI_Commctx *commctx = (PyMPI_Commctx *)v;
    if (commctx->localcomm != MPI_COMM_NULL)
      (void)MPI_Comm_free(&commctx->localcomm);
    if (commctx->dupcomm != MPI_COMM_NULL)
      (void)MPI_Comm_free(&commctx->dupcomm);
    PyMPI_FREE(commctx);
  }
  return MPI_SUCCESS;
}

static int PyMPI_Commctx_keyval(int *keyval)
{
  static int comm_keyval = MPI_KEYVAL_INVALID;
  if (comm_keyval == MPI_KEYVAL_INVALID) {
    int ierr, self_keyval = MPI_KEYVAL_INVALID;
    ierr = MPI_Comm_create_keyval(MPI_COMM_NULL_COPY_FN, PyMPI_Commctx_free_fn, &comm_keyval, NULL); CHKERR(ierr);
    ierr = MPI_Comm_create_keyval(MPI_COMM_NULL_COPY_FN, PyMPI_Commctx_free_fn, &self_keyval, &comm_keyval); CHKERR(ierr);
    ierr = MPI_Comm_set_attr(MPI_COMM_SELF, self_keyval, NULL); CHKERR(ierr);
  }
  if (keyval) *keyval = comm_keyval;
  return MPI_SUCCESS;
}

static int PyMPI_Commctx_lookup(MPI_Comm comm, PyMPI_Commctx **_commctx)
{
  int ierr, found = 0, keyval = MPI_KEYVAL_INVALID;
  PyMPI_Commctx *commctx = NULL;

  ierr = PyMPI_Commctx_keyval(&keyval); CHKERR(ierr);
  ierr = MPI_Comm_get_attr(comm, keyval, &commctx, &found); CHKERR(ierr);
  if (found) goto fn_exit;

  ierr = PyMPI_Commctx_new(&commctx); CHKERR(ierr);
  if (!commctx) return (void)MPI_Comm_call_errhandler(comm, MPI_ERR_INTERN), MPI_ERR_INTERN;
  ierr = MPI_Comm_set_attr(comm, keyval, commctx); CHKERR(ierr);
  ierr = MPI_Comm_dup(comm, &commctx->dupcomm); CHKERR(ierr);

 fn_exit:
  if (commctx->tag >= commctx->tag_ub) commctx->tag = 0;
  if (_commctx) *_commctx = commctx;
  return MPI_SUCCESS;
}

static int PyMPI_Commctx_intra(MPI_Comm comm, MPI_Comm *dupcomm, int *tag)
{
  int ierr;
  PyMPI_Commctx *commctx = NULL;
  ierr = PyMPI_Commctx_lookup(comm, &commctx);CHKERR(ierr);
  if (dupcomm)
    *dupcomm = commctx->dupcomm;
  if (tag)
    *tag = commctx->tag++;
  return MPI_SUCCESS;
}

static int PyMPI_Commctx_inter(MPI_Comm comm, MPI_Comm *dupcomm, int *tag,
                               MPI_Comm *localcomm, int *low_group)
{
  int ierr;
  PyMPI_Commctx *commctx = NULL;
  ierr = PyMPI_Commctx_lookup(comm, &commctx);CHKERR(ierr);
  if (commctx->localcomm == MPI_COMM_NULL) {
    int localsize, remotesize, mergerank;
    MPI_Comm mergecomm = MPI_COMM_NULL;
    MPI_Group localgroup = MPI_GROUP_NULL;
    ierr = MPI_Comm_size(comm, &localsize); CHKERR(ierr);
    ierr = MPI_Comm_remote_size(comm, &remotesize); CHKERR(ierr);
    ierr = MPI_Intercomm_merge(comm, localsize>remotesize, &mergecomm); CHKERR(ierr);
    ierr = MPI_Comm_rank(mergecomm, &mergerank); CHKERR(ierr);
    ierr = MPI_Comm_group(comm, &localgroup); CHKERR(ierr);
    ierr = MPI_Comm_create(mergecomm, localgroup, &commctx->localcomm); CHKERR(ierr);
    ierr = MPI_Group_free(&localgroup); CHKERR(ierr);
    ierr = MPI_Comm_free(&mergecomm); CHKERR(ierr);
    commctx->low_group = (localsize>remotesize) ? 0 :
                         (localsize<remotesize) ? 1 :
                         (mergerank<localsize);
  }
  if (dupcomm)
    *dupcomm = commctx->dupcomm;
  if (tag)
    *tag = commctx->tag++;
  if (localcomm)
    *localcomm = commctx->localcomm;
  if (low_group)
    *low_group  = commctx->low_group;
  return MPI_SUCCESS;
}

#undef CHKERR

/*
   Local variables:
   c-basic-offset: 2
   indent-tabs-mode: nil
   End:
*/
