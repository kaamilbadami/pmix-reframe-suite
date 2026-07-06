/*
 * Usage:  sleeper NSECONDS [LAUNCHID]
 */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <mpi.h>

#define NUM_SEC   1  /* default number of seconds */
#define ID_NUM    1  /* default launch id number */

void usage(void)
{
    char *name = "sleeper";

    printf("Usage: %s  [-n SECONDS]  [-i NUMBER]\n", name);
    printf("  -n SECONDS    Number of seconds (postive integer, default: %d)\n", NUM_SEC);
    printf("  -i NUMBER     Identifier number (positive integer, default: %d)\n", ID_NUM);
    return;
}

int main(int argc, char **argv)
{
    char host[128];
    char outfile[128];
    int rc;
    int nsec = NUM_SEC;
    int launchid = ID_NUM;
    pid_t pid = getpid();
    FILE *fp = NULL;
    int opt, mpi_rank, mpi_size, number;

    MPI_Init(&argc, &argv);

    while ((opt = getopt(argc, argv, "hn:i:")) != -1) {
        switch (opt) {
        case 'n':
            /* Number of seconds */
            nsec = atoi(optarg);
            break;
        case 'i':
            /* Identifier number*/
            launchid = atoi(optarg);
            break;
        case 'h':
            /* Show help and exit */
            usage();
            exit (EXIT_SUCCESS);
        }
    }

    if (0 > (rc = gethostname(host, sizeof(host)))) {
        fprintf(stderr, "(%6d) Error: failed to obtain hostname (rc=%d)\n", pid, rc);
        return (1);
    }

    MPI_Comm_rank(MPI_COMM_WORLD, &mpi_rank);
    MPI_Comm_size(MPI_COMM_WORLD, &mpi_size);

//    sprintf(outfile, "%s/output.sleeper.%s.%d.%d.txt",
//                     "/tmp",
//                     host, launchid, pid);

//    if (NULL == (fp = fopen(outfile, "wx"))) {
//        fprintf(stderr, "(%6d) Error: failed to open output file (rc=%d)\n", pid, rc);
//        return (1);
//    }
    /*
    MPI_Comm_rank(MPI_COMM_WORLD, &mpi_rank);
    MPI_Comm_size(MPI_COMM_WORLD, &mpi_size);

    if (mpi_rank%2 == 0) {
	number = -1;
	MPI_Send(&number, 1, MPI_INT, mpi_rank+1, 0, MPI_COMM_WORLD);
    } else if (mpi_rank%2 == 1) {
	MPI_Recv(&number, 1, MPI_INT, mpi_rank-1, 0, MPI_COMM_WORLD,
		 MPI_STATUS_IGNORE);
	fprintf(stdout, "Process 1 received number %d from process 0\n",
	       number);
    }
    */
    sleep(nsec);

    //fprintf(fp, "(%06d.%6d) [%s] DONE (slept %d seconds)\n", launchid, pid, host, nsec);
    fprintf(stdout, "(%06d.%6d) [%s] %2d %2d  DONE (slept %d seconds)\n", launchid, pid, host, mpi_rank, mpi_size, nsec);

    //fprintf(stdout, "MPI report, %i, %i\n", mpi_rank, mpi_size);

    //fflush(NULL);
    //fclose(fp);

    MPI_Finalize();

    return 0;
}
